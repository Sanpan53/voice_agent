"""需求澄清 Agent 的结构化契约。"""

from enum import StrEnum

from pydantic import Field

from app.schemas.common import CamelModel


class ClarifyAction(StrEnum):
    """澄清决策：继续追问，还是信息已足可以直接推荐。"""

    ASK = "ASK"
    READY = "READY"


class ClarifyResult(CamelModel):
    """澄清决策结果。

    Attributes:
        action: ``ASK`` 表示还要追问，``READY`` 表示可以进推荐。
        question_to_ask: 追问话术；``READY`` 时为 ``None``。
        missing_slots: 缺失的槽位名列表；``READY`` 时为空。
    """

    action: ClarifyAction
    question_to_ask: str | None = None
    missing_slots: list[str] = Field(default_factory=list)

    @property
    def is_ask(self) -> bool:
        """是否还需要追问（Orchestrator 分叉用，读起来比 ``action == ASK`` 顺）。"""
        return self.action == ClarifyAction.ASK

    @classmethod
    def ready(cls) -> "ClarifyResult":
        """信息已足，直接进推荐。"""
        return cls(action=ClarifyAction.READY, question_to_ask=None, missing_slots=[])

    @classmethod
    def ask(cls, question: str, missing: list[str]) -> "ClarifyResult":
        """需要追问。"""
        return cls(action=ClarifyAction.ASK, question_to_ask=question, missing_slots=list(missing))
