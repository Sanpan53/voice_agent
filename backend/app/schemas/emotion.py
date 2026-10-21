"""情感应答契约。"""

from pydantic import Field

from app.schemas.common import CamelModel
from app.schemas.recommend import RecommendedItem


class EmotionResult(CamelModel):
    """情感 Agent 的最终输出。

    Attributes:
        speech_text: 给 TTS 的口语文本。**语音版本追求精简口语**。
        display_blocks: 给前端 UI 的商品卡片，直接复用推荐 Agent 出来的商品列表。
            **视觉版本可以信息量大**——两者分离是刻意设计。
    """

    speech_text: str
    display_blocks: list[RecommendedItem] = Field(default_factory=list)
