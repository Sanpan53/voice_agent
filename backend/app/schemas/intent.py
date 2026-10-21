"""意图理解 Agent 的结构化契约。"""

from enum import StrEnum
from typing import Any

from pydantic import Field

from app.schemas.common import CamelModel


class IntentType(StrEnum):
    """意图分类。

    继承 ``StrEnum`` 让成员本身就是字符串，既能写
    ``IntentType.PRODUCT_RECOMMENDATION``，又能直接参与 JSON 序列化比较。
    """

    PRODUCT_RECOMMENDATION = "PRODUCT_RECOMMENDATION"  # 明确型商品需求
    CLARIFY_NEEDED = "CLARIFY_NEEDED"  # 模糊型需求
    PRODUCT_COMPARE = "PRODUCT_COMPARE"  # 商品对比
    CHITCHAT = "CHITCHAT"  # 闲聊
    ORDER_CONFIRM = "ORDER_CONFIRM"  # 下单确认
    OUT_OF_SCOPE = "OUT_OF_SCOPE"  # 越权 / 异常


#: JSON 解析失败时的降级置信度
FALLBACK_CONFIDENCE = 0.3

#: 模型没给 confidence 时的默认值
DEFAULT_CONFIDENCE = 0.5


class IntentResult(CamelModel):
    """Intent Agent 的结构化输出。

    Attributes:
        intent: 分类到的意图。
        slots: 从用户话里抽到的槽位（category/budget/scenario 等），抽不到的填 ``None``。
        confidence: 模型给出的置信度 ``[0.0, 1.0]``，JSON 解析失败降级时会填 0.3。
    """

    intent: IntentType
    slots: dict[str, Any] = Field(default_factory=dict)
    confidence: float = DEFAULT_CONFIDENCE

    @classmethod
    def fallback(cls) -> "IntentResult":
        """解析失败时的兜底结果：``OUT_OF_SCOPE`` + 空槽位 + 0.3。

        Orchestrator 收到它会自然走礼貌拒绝分支。
        """
        return cls(intent=IntentType.OUT_OF_SCOPE, slots={}, confidence=FALLBACK_CONFIDENCE)
