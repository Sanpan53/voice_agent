"""需求澄清 Service——判断槽位够不够推荐，不够就生成一个自然的追问。

职责一句话总结：**规则判断 + LLM 包装**。
"""

import json
import logging

from agentscope.message import Msg

from app.agent.agent_factory import AgentFactory
from app.schemas.clarify import ClarifyResult
from app.services.clarify_rule_service import ClarifyRuleService

logger = logging.getLogger(__name__)

#: 一次最多问几个字段。Prompt 层有同样的约束，但那是软约束，Service 层兜一遍。
MAX_MISSING_SLOTS = 2

USER_MESSAGE_TEMPLATE = "用户原话：{utterance}\n已知信息：{slots}\n缺失字段：{missing}"


class ClarifyService:
    """需求澄清决策。

    Args:
        factory: Agent 工厂。
        rule_service: 品类必填槽位表。
    """

    def __init__(self, factory: AgentFactory, rule_service: ClarifyRuleService) -> None:
        self._factory = factory
        self._rule_service = rule_service

    async def decide(
        self,
        session_id: str,
        utterance: str,
        slots: dict | None,
    ) -> ClarifyResult:
        """判断该追问还是可以直接推荐。"""
        slots = slots or {}
        category = slots.get("category")
        if category is not None and not isinstance(category, str):
            category = str(category)

        missing = self._rule_service.missing_slots(category, slots)
        if not missing:
            # 必填槽位齐全，直接进推荐，不浪费一次 LLM 调用
            return ClarifyResult.ready()

        # 兜底：一次最多问 2 个字段，超过就只把最重要的两个送进 Prompt。
        # 语音场景用户听到"请问你的预算、使用场景、品牌偏好、尺码、送货方式是什么"
        # 立刻就崩——信息密度过高，用户记不住。把裁剪放在发 LLM **之前**做，
        # LLM 压根看不到多余字段，从源头避免它生成长追问；返回给 Orchestrator 的
        # missingSlots 也是裁剪后的，SessionState 记录"问过哪些槽位"的逻辑不会被污染。
        if len(missing) > MAX_MISSING_SLOTS:
            missing = missing[:MAX_MISSING_SLOTS]

        agent = self._factory.get(session_id).clarify
        user_message = USER_MESSAGE_TEMPLATE.format(
            utterance=utterance,
            slots=self._write_json(slots),
            missing=missing,
        )
        response: Msg = await agent(Msg("user", user_message, "user"))

        return ClarifyResult.ask((response.get_text_content() or "").strip(), missing)

    @staticmethod
    def _write_json(value: object) -> str:
        """槽位转 JSON 文本；序列化失败时退化成 ``str()``。"""
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)
