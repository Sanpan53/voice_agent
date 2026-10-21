"""商品推荐契约。

这两个结构会在候选生成、画像加权、理由生成、最终出口之间来回传，用不可变结构 +
``with...`` 模式保证"每次改动都产生新实例"。Python 侧用 Pydantic 模型 +
:meth:`~pydantic.BaseModel.model_copy` 表达同一件事。
"""

from typing import Any

from pydantic import Field

from app.schemas.common import CamelModel

#: explanationTone：有结果时提示情感 Agent 用专业口吻
TONE_PROFESSIONAL = "professional"

#: explanationTone：空结果兜底，提示情感 Agent 生成"没找到合适的"而不是硬推
TONE_EMPTY = "empty"

#: explanationTone：闲聊分支。``items`` 必然为空，情感 Agent 据此
#: 走"聊天模式"而不是"推荐播报模式"。
TONE_CHITCHAT = "chitchat"


class RecommendedItem(CamelModel):
    """一条推荐候选。

    Attributes:
        product_id: 商品 ID。
        name: 商品名。
        price: 价格。
        reason: 推荐理由；候选阶段为 ``None``，理由生成阶段回填。
        match_score: 匹配分；候选阶段是向量检索顺序分，加权后是综合分。
        attributes: 扩展属性（``category_l2`` / ``brand`` / 颜色 / 适用场景等）。
    """

    product_id: int
    name: str
    price: float
    reason: str | None = None
    match_score: float
    attributes: dict[str, Any] = Field(default_factory=dict)

    def with_match_score(self, score: float) -> "RecommendedItem":
        """加权后返回新实例。"""
        return self.model_copy(update={"match_score": score})

    def with_reason(self, reason: str) -> "RecommendedItem":
        """回填理由后返回新实例。"""
        return self.model_copy(update={"reason": reason})


class RecommendResult(CamelModel):
    """推荐 Agent 的最终输出，交给情感 Agent 做口语化包装。"""

    items: list[RecommendedItem] = Field(default_factory=list)
    explanation_tone: str = TONE_PROFESSIONAL

    @classmethod
    def empty(cls) -> "RecommendResult":
        """空结果兜底：三步放宽都召回不到时的返回值。"""
        return cls(items=[], explanation_tone=TONE_EMPTY)
