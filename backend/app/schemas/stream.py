"""流式下发的分片契约。

一条用户输入在流式模式下不再产出**一个** ``EmotionResult``，而是产出**一串**
分片：商品卡片先到、然后是逐句的文字、文字后面各自跟着自己的音频。

它用 ``dataclass`` 而不是 Pydantic 模型——**不是对外契约**，只在进程内从
Orchestrator 流到 WebSocket handler，真正的线格式
（``{"type": "caption", ...}`` / 二进制音频帧）在
:mod:`app.api.voice_ws` 里拼。

**为什么不做成"三种事件类"**：下游是一个 ``async for`` 循环，三种类型意味着
三处 ``isinstance`` 分支或者一个 ``match``。带 ``kind`` 的单一类型让
"顺序下发、顺序消费"这件事保持直白。
"""

from dataclasses import dataclass, field
from enum import StrEnum

from app.schemas.recommend import RecommendedItem


class StreamKind(StrEnum):
    """分片类型。"""

    TEXT = "text"
    AUDIO = "audio"
    PRODUCTS = "products"


@dataclass(frozen=True)
class StreamChunk:
    """流式下发的一个分片。三个"载荷"字段里只有一个会被填充。"""

    kind: StreamKind
    text: str = ""
    audio: bytes = b""
    products: tuple[RecommendedItem, ...] = field(default=())

    @classmethod
    def text_chunk(cls, text: str) -> "StreamChunk":
        """一段口播文字。

        .. note::

           方法名带 ``_chunk`` 后缀是**被迫的**：dataclass 的字段就叫 ``text``，
           同名类方法会把它整个盖掉，实例上再也读不到 ``.text``。
        """
        return cls(kind=StreamKind.TEXT, text=text)

    @classmethod
    def audio_chunk(cls, audio: bytes) -> "StreamChunk":
        """一帧 PCM 音频。"""
        return cls(kind=StreamKind.AUDIO, audio=audio)

    @classmethod
    def products_chunk(cls, items: list[RecommendedItem]) -> "StreamChunk":
        """一批商品卡片。

        ``list`` 进 ``tuple`` 出——frozen dataclass 里塞可变列表，等于"不可变"
        只写在脸上；转成元组至少堵住了"下游顺手 append 一下"这条路。
        """
        return cls(kind=StreamKind.PRODUCTS, products=tuple(items))
