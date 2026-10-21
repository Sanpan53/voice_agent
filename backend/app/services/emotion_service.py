"""情感应答 Service。

把 :class:`~app.schemas.recommend.RecommendResult` 包装成一段自然的口语回复，
供 TTS 合成朗读。**这是流水线的最后一站**——再往下就是声音了。

这个 Service 的职责变重了：**推荐理由生成被合并进来了**。原先
"推荐 Agent 写理由（~800ms）+ 情感 Agent 包装（~600ms）"是两次 LLM 调用，
现在只留一次——情感 Agent 直接拿**裸商品数据**（带 ``attributes``），
自己看属性编理由。省下的就是一次完整的模型 round-trip。
"""

import json
import logging
import re
from typing import Any

from agentscope.message import Msg

from app.agent.agent_factory import AgentFactory
from app.agent.agent_memory_policy import AgentMemoryPolicy
from app.schemas.emotion import EmotionResult
from app.schemas.recommend import RecommendResult
from app.services.session_mood_detector import SessionMoodDetector

logger = logging.getLogger(__name__)

#: 空结果兜底文案
EMPTY_FALLBACK = "鸡哥帮你找了一下，这个条件下合适的不多，要不要放宽点预算再看看？"

#: 模板兜底的固定开场与收尾
FALLBACK_OPENING = "好，鸡哥给你挑了几款。"
FALLBACK_CLOSING = "你看看选哪个？"

#: 中文数字
_DIGITS = "零一二三四五六七八九"

#: 去掉 ```json ... ``` 之类的 Markdown 包裹。
#:
#: 两条正则分别锚定开头和结尾。``re.DOTALL`` 是必需的——
#: **少了它，``^`` 和 ``$`` 只认首尾行，被包裹的整段文本就剥不掉**。
_FENCE_OPEN = re.compile(r"^```(?:json|text)?\s*", re.DOTALL)
_FENCE_CLOSE = re.compile(r"\s*```$", re.DOTALL)


def _ordinal(index: int) -> str:
    """1 -> ``一``，10 -> ``十``，11 -> ``十一``，21 -> ``二十一``。

    模板兜底改用中文数字是**有意的**：这个兜底是 LLM 输出文案的替身，而 Prompt 明确要求
    口播里说"第一款 / 第二款 / 第三款"。用户不该听得出来这句话是模型写的还是兜底
    拼的——风格一致才算兜得住。
    """
    if index <= 0:
        return str(index)
    if index < 10:
        return _DIGITS[index]
    if index == 10:
        return "十"
    if index < 20:
        return "十" + _DIGITS[index % 10]
    tens, ones = divmod(index, 10)
    return _DIGITS[tens] + "十" + (_DIGITS[ones] if ones else "")


def sanitize(raw: str | None) -> str:
    """剥掉 Markdown 代码块包裹并去掉首尾空白。

    **契约变了**：Prompt 的输出现在是**纯文本**，不再是 ``{"speechText": ...}``，
    所以这里只做"剥壳"，不做 JSON 解析。旧版的 :func:`extract_json_object`
    连同 ``_parse_speech`` 一起删掉了——留着一个已经没人调用的 JSON 解析器，
    只会让后来人以为输出契约还是 JSON。
    """
    if raw is None:
        return ""
    text = _FENCE_OPEN.sub("", raw.strip())
    text = _FENCE_CLOSE.sub("", text)
    return text.strip()


def build_products_payload(rec: RecommendResult) -> list[dict[str, Any]]:
    """商品列表 → 喂给模型的"裸数据"。

    这里**显式投影**四个字段，理由有两个：

    - 这里的关键就是"理由由模型自己编"，再带上 ``reason``（此时恒为 ``None``）
      只会让 Prompt 里"没有 reason 字段"这句话自相矛盾；
    - ``matchScore`` 是排序中间产物，对"怎么说话"没有任何帮助，纯粹是干扰。

    ``attributes`` 必须带上——**它是模型编理由的唯一依据**。
    """
    return [
        {
            "productId": item.product_id,
            "name": item.name,
            "price": item.price,
            "attributes": item.attributes,
        }
        for item in rec.items
    ]


class EmotionService:
    """口语化包装器。"""

    def __init__(
        self,
        factory: AgentFactory,
        mood_detector: SessionMoodDetector,
        memory_policy: AgentMemoryPolicy | None = None,
    ) -> None:
        self._factory = factory
        self._mood_detector = mood_detector
        self._memory_policy = memory_policy or AgentMemoryPolicy()

    async def wrap(
        self,
        session_id: str,
        user_utterance: str,
        user_needs: str,
        rec: RecommendResult,
    ) -> EmotionResult:
        """把推荐结果包装成口语回复。

        ``display_blocks`` 直接复用推荐 Agent 出来的商品列表——**无论**口播文案生成
        成功与否都原样透出，前端卡片不受模型波动影响。

        Args:
            session_id: 会话 ID。
            user_utterance: 用户原话（可能已被点评团意见拼长，见
                :meth:`~app.services.orchestrator_service.OrchestratorService._handle_recommendation`）。
            user_needs: 由意图槽位拼成的需求描述，**闲聊场景传空串**。
            rec: 推荐结果。``items`` 为空只表示"这次没商品"，**不代表要说什么**——
                说"没找到合适的"还是"陪你聊聊"，由 ``explanation_tone`` 决定。
        """
        mood = await self._mood_detector.detect(session_id, user_utterance)

        payload: dict[str, Any] = {
            "userUtterance": user_utterance,
            "sessionMood": str(mood),
            "userNeeds": user_needs,
            # 场景标记：professional（推荐播报）/ empty（空结果）/ chitchat（闲聊）。
            #
            # **``explanationTone`` 必须传下去**：只考虑推荐播报一种场景的话，
            # ``handle_chitchat`` 传进去的空商品列表会和 Prompt 的"products 是空数组
            # → 说没找到合适的"这条规则**撞车**：用户说"你好"，收到的是"这个条件下
            # 合适的不多，要不要放宽点预算"。
            # 判定见 ``test_chitchat_does_not_get_the_empty_result_copy``。
            "scene": rec.explanation_tone,
            "products": build_products_payload(rec),
        }

        agent = self._factory.get(session_id).emotion

        # 情绪是连续的——保留相对长的历史（40 条 ≈ 20 轮）用于情绪连续性判断
        await self._memory_policy.before_emotion_call(agent)

        try:
            response: Msg = await agent(
                Msg("user", json.dumps(payload, ensure_ascii=False), "user")
            )
            speech = sanitize(response.get_text_content())
        except Exception:
            logger.warning("EmotionAgent 解析失败，降级为模板文案", exc_info=True)
            return EmotionResult(speech_text=fallback(rec), display_blocks=rec.items)

        if not speech:
            logger.warning("EmotionAgent 返回空文本，降级为模板文案")
            return EmotionResult(speech_text=fallback(rec), display_blocks=rec.items)

        return EmotionResult(speech_text=speech, display_blocks=rec.items)


def fallback(rec: RecommendResult) -> str:
    """模板兜底文案。

    宁可给一句**平淡但正确**的模板，也不能让用户听到静音——口播失败时这是唯一
    的保底。

    Note:
        闲聊 / 空结果场景下 ``items`` 是空的，直接返回 :data:`EMPTY_FALLBACK`。
        对闲聊来说这句话文不对题，但它是**兜底的兜底**（模型挂了才会走到这里），
        总好过静音。
    """
    if not rec.items:
        return EMPTY_FALLBACK

    parts = [FALLBACK_OPENING]
    for index, item in enumerate(rec.items, start=1):
        # reason 现在恒为 None（理由改由情感 Agent 现场编）——拼字符串时退化成空。
        # 保留这条分支是为了兜底路径本身够健壮：将来若回滚到"先写理由再包装"的
        # 两段式，这里不用改。
        reason = item.reason or ""
        parts.append(f"第{_ordinal(index)}款：{item.name}，{reason}。")
    parts.append(FALLBACK_CLOSING)
    return "".join(parts)
