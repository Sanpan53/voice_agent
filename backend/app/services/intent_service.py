"""意图识别 Service——把 Intent Agent 封成对 Orchestrator 友好的纯方法。

Orchestrator 不用关心怎么调 Agent、怎么解析 JSON，只调
:meth:`IntentService.classify`。
"""

import json
import logging
from typing import Any

from agentscope.message import Msg

from app.agent.agent_factory import AgentFactory
from app.agent.agent_memory_policy import AgentMemoryPolicy
from app.memory.short_term_memory import ShortTermMemory
from app.schemas.intent import DEFAULT_CONFIDENCE, IntentResult, IntentType
from app.services import common_confirmer
from app.services.intent_cache import IntentCache

logger = logging.getLogger(__name__)

#: 拼进用户消息的历史轮数
HISTORY_TURNS = 3

#: 无历史时的占位文案
NO_HISTORY = "（无历史）"

USER_INPUT_TEMPLATE = """最近 3 轮对话摘要：
{history}

当前这一句：
{utterance}
"""


def extract_json(text: str) -> str:
    """从模型输出里抠出 JSON 部分。

    LLM 偶尔会输出 ``好的，这是结果：{...}``，用简单的 ``{...}`` 切片避免解析崩。
    找不到花括号时原样返回，交给调用方走降级分支。
    """
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start:end + 1]
    return text


class IntentService:
    """意图分类与槽位抽取。

    Args:
        factory: Agent 工厂（按 sessionId 取 Agent 实例）。
        memory: 短期记忆（取最近 3 轮对话摘要消解指代）。
        cache: 意图结果缓存，传 ``None`` 表示不启用缓存。
        memory_policy: Agent 内部记忆清理策略。传 ``None`` 或
            不传时退化成"每轮清空"，与之前的硬编码行为一致。
    """

    def __init__(
        self,
        factory: AgentFactory,
        memory: ShortTermMemory,
        cache: IntentCache | None = None,
        memory_policy: AgentMemoryPolicy | None = None,
    ) -> None:
        self._factory = factory
        self._memory = memory
        self._cache = cache
        # 默认策略 = 每轮清空。原先这里写死了 ``await agent.memory.clear()``，
        # 现在挪到 Policy 里统一管理，默认行为不变。
        self._memory_policy = memory_policy or AgentMemoryPolicy()

    async def classify(self, session_id: str, utterance: str) -> IntentResult:
        """把用户这一句话分类到六种意图之一，并抽取槽位。"""
        # -1) 极短确认词直接走进程内常量，连 Redis 都省了
        confirmer = common_confirmer.match(utterance)
        if confirmer is not None:
            logger.debug(
                "[Intent] 命中 confirmer 短路 sessionId=%s utterance=%s", session_id, utterance
            )
            # **不写回 IntentCache**：本来就是常量返回，写缓存只是白占一个 key。
            return confirmer

        # 0) 再查缓存（指纹版）——5 分钟内同一会话说同样的话，跳过 LLM
        if self._cache is not None:
            cached = await self._cache.get(session_id, utterance)
            if cached is not None:
                logger.debug("[Intent] 命中缓存 sessionId=%s utterance=%s", session_id, utterance)
                return cached

        agent = self._factory.get(session_id).intent

        # 意图识别是无状态单轮任务，每次调用前清空上一轮残留的对话历史，避免误判。
        # 这一步收进 AgentMemoryPolicy，由策略统一管理。
        await self._memory_policy.before_intent_call(agent)

        history = await self._recent_history(session_id)

        # 把历史和当前这一句拼进用户消息——sys_prompt 在构造时就定死了，没法动态改
        user_input = USER_INPUT_TEMPLATE.format(history=history, utterance=utterance)
        response: Msg = await agent(Msg("user", user_input, "user"))

        result = self._parse(response.get_text_content() or "")

        # 1) 解析失败的 OUT_OF_SCOPE 不写缓存，避免一次抖动把所有"相似的话"都污染成越权
        if self._cache is not None and result.intent is not IntentType.OUT_OF_SCOPE:
            await self._cache.put(session_id, utterance, result)
        return result

    async def _recent_history(self, session_id: str) -> str:
        """最近 3 轮对话摘要，格式 ``role: text`` 逐行拼接。"""
        turns = await self._memory.recent(session_id, HISTORY_TURNS)
        if not turns:
            return NO_HISTORY
        return "\n".join(f"{turn.role}: {turn.text}" for turn in turns)

    @staticmethod
    def _parse(text: str) -> IntentResult:
        """解析模型输出；任何异常都降级为 ``OUT_OF_SCOPE``。"""
        try:
            parsed: Any = json.loads(extract_json(text))
            intent = IntentType(parsed["intent"])
            slots = parsed.get("slots") or {}
            if not isinstance(slots, dict):
                slots = {}
            confidence = float(parsed.get("confidence", DEFAULT_CONFIDENCE))
        except Exception:  # noqa: BLE001 — 解析失败是预期内的降级路径
            logger.warning("意图 JSON 解析失败，降级为 OUT_OF_SCOPE：%s", text)
            return IntentResult.fallback()

        return IntentResult(intent=intent, slots=slots, confidence=confidence)
