"""推荐理由生成。

把 Top-N 候选连同用户需求一起丢给主模型，一次调用批量产出每款的推荐理由。

.. important::

   **这个 Service 已经不在主链路上了**，但代码和测试都完整保留——
   它和 ``prompts/emotion.txt`` 是一组**回滚资产**（保留以备回滚）。

   摘掉它的原因很直接：它做的是"把属性编成理由"，而情感 Agent 做的是
   "把理由编成一段话"。两次 ``qwen-max`` 调用，合计 ~1400ms，做的是同一件事的
   上下半段。合并成一次之后省掉整整一个 round-trip（~800ms）。

   所以 :class:`~app.services.recommend_orchestrator.RecommendOrchestrator` 与
   :class:`~app.services.parallel_recommend_service.ParallelRecommendService`
   **既不接收也不调用它**（这一点由 ``tests/test_parallel_recommend.py`` 里的结构性
   断言守着——光是"记得别调"是不够的）。要回滚：把它们改回接收本 Service、
   在返回前调 :meth:`attach_reasons`，再把情感 Agent 的 Prompt 换回
   ``prompts/emotion.txt``。
"""

import json
import logging
from typing import Any

from agentscope.message import Msg

from app.agent.agent_factory import AgentFactory
from app.agent.agent_memory_policy import AgentMemoryPolicy
from app.schemas.recommend import RecommendedItem

logger = logging.getLogger(__name__)


def extract_json_array(text: str) -> str:
    """从模型输出里抠出 JSON 数组部分。

    注意用的是 ``[`` / ``]``（不是 :func:`app.services.intent_service.extract_json`
    的 ``{`` / ``}``）——本 Prompt 的输出契约是**数组**。LLM 偶尔会输出
    ``好的，这是推荐理由：[...]``，切片后再解析避免整条链路崩掉。
    """
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return text


class RecommendReasonService:
    """给候选商品批量生成推荐理由。

    Note:
        ``RecommendOrchestrator`` 本身不直接持有 Agent（它只编排
        candidates / reranker / reasonService），所以记忆裁剪策略挂在**这里**
        才命中真正的 recommend Agent。
    """

    def __init__(
        self, factory: AgentFactory, memory_policy: AgentMemoryPolicy | None = None
    ) -> None:
        self._factory = factory
        self._memory_policy = memory_policy or AgentMemoryPolicy()

    async def attach_reasons(
        self,
        session_id: str,
        user_needs: str,
        products: list[RecommendedItem],
    ) -> list[RecommendedItem]:
        """返回回填了 ``reason`` 的新列表。

        失败时**降级为空理由**并原样返回商品——理由只是锦上添花，不能因为它挂了就
        让整个推荐链路返回 500。
        """
        if not products:
            return products

        agent = self._factory.get(session_id).recommend

        # 裁到最近 4 轮——避免 Token 线性增长，也避免历史推荐污染新推荐
        await self._memory_policy.before_recommend_call(agent)

        payload = {
            "userNeeds": user_needs,
            "products": [
                {
                    "productId": p.product_id,
                    "name": p.name,
                    "price": p.price,
                    "attributes": p.attributes,
                }
                for p in products
            ],
        }

        try:
            response: Msg = await agent(
                Msg("user", json.dumps(payload, ensure_ascii=False), "user")
            )
        except Exception:
            logger.warning("推荐理由生成失败，降级为空理由", exc_info=True)
            return products

        reasons = self._parse(response.get_text_content() or "")
        if reasons is None:
            return products

        return [p.with_reason(reasons.get(p.product_id, "")) for p in products]

    @staticmethod
    def _parse(text: str) -> dict[int, str] | None:
        """解析模型输出，得到 ``{productId: reason}``；整体无法解析时返回 ``None``。

        ``None`` 与空字典是**两种不同的降级**，调用方要区别对待：

        - ``None``——整段输出不是可解析的 JSON 数组（模型跑题、超时截断）。返回
          原始商品，``reason`` 保持 ``None``，语义与"捕获异常后原样返回商品"一致。
        - ``{}`` 或部分缺失——数组解析成功了，只是没覆盖到某些商品。这些商品拿到空
          字符串，说明"这次没生成出理由"。

        单条结构不合法只跳过那一条，不整批作废——按条容错比整批丢弃更稳妥。
        """
        try:
            parsed: Any = json.loads(extract_json_array(text))
        except (TypeError, ValueError):
            logger.warning("推荐理由 JSON 解析失败，降级为空理由：%s", text)
            return None

        if not isinstance(parsed, list):
            logger.warning("推荐理由输出不是数组，降级为空理由：%s", text)
            return None

        reasons: dict[int, str] = {}
        for entry in parsed:
            if not isinstance(entry, dict):
                continue
            product_id = entry.get("productId")
            reason = entry.get("reason")
            # bool 是 int 的子类，显式排除，避免 True 被当成 productId=1
            if isinstance(product_id, bool) or not isinstance(product_id, (int, float)):
                continue
            reasons[int(product_id)] = reason if isinstance(reason, str) else ""
        return reasons
