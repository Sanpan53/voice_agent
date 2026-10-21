"""用户讲话事件：非 Agent 监听者的广播。

**MsgHub 装不下这些监听者**——它的 ``participants`` 只能是 Agent 对象。画像预热、
审计日志、Prometheus 指标、TTS 预热这些都是普通 Service，走事件广播这条路。

Python 没有容器级事件总线，这里用**最小实现**：一个持有订阅者列表的 Publisher，
``publish`` 时把事件分发给所有订阅者。默认走 ``asyncio.create_task`` 异步派发，
单个监听者抛异常不影响其他监听者，也不影响发布方。
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UserSpokenEvent:
    """用户说完一句话（ASR 出结果）时发布的事件。"""

    session_id: str
    user_id: int
    utterance: str
    timestamp: float


#: 监听者签名：``async def handler(event) -> None``
EventHandler = Callable[[UserSpokenEvent], Awaitable[None]]


class VoiceEventPublisher:
    """极简事件发布器。

    Args:
        handlers: 订阅者列表。为空时 :meth:`publish_user_spoken` 是空操作——
            这让"事件系统没装"的场景天然安全。
    """

    def __init__(self, handlers: list[EventHandler] | None = None) -> None:
        self._handlers: list[EventHandler] = list(handlers or [])

    def subscribe(self, handler: EventHandler) -> None:
        """注册一个监听者。"""
        self._handlers.append(handler)

    @property
    def handler_count(self) -> int:
        """当前监听者数量（观测 / 测试用）。"""
        return len(self._handlers)

    def publish_user_spoken(
        self, session_id: str, user_id: int, utterance: str, timestamp: float
    ) -> asyncio.Task[None] | None:
        """发布"用户讲话"事件，异步派发给所有监听者。

        Returns:
            派发任务；没有监听者时返回 ``None``。调用方**不需要 await**——
            这正是异步事件的意义：主请求立刻返回，监听者在后台跑。
        """
        if not self._handlers:
            return None

        event = UserSpokenEvent(
            session_id=session_id,
            user_id=user_id,
            utterance=utterance,
            timestamp=timestamp,
        )
        return asyncio.create_task(self._dispatch(event))

    async def _dispatch(self, event: UserSpokenEvent) -> None:
        """并发调用所有监听者，逐个兜住异常。

        用 ``return_exceptions=True`` 让监听者互不影响——
        **一个监听者挂掉不能拖垮另一个**。
        """
        results = await asyncio.gather(
            *(handler(event) for handler in self._handlers),
            return_exceptions=True,
        )
        for handler, result in zip(self._handlers, results, strict=True):
            if isinstance(result, Exception):
                logger.warning(
                    "事件监听者执行失败 handler=%s", getattr(handler, "__name__", handler),
                    exc_info=result,
                )


class VoiceEventListeners:
    """"用户讲话"事件的两个内置监听者。

    Args:
        profile_service: 画像服务，用于预热缓存。
    """

    def __init__(self, profile_service: Any) -> None:
        self._profile_service = profile_service

    async def on_user_spoken_warmup(self, event: UserSpokenEvent) -> None:
        """画像预热：提前读一次，把快照灌进 Redis 缓存。

        推荐 Agent 随后读画像就是热读，省一次数据库往返。
        """
        try:
            await self._profile_service.load(event.user_id)
            logger.info("[WARMUP] userId=%s 画像已预热", event.user_id)
        except Exception:
            logger.warning("画像预热失败 userId=%s", event.user_id, exc_info=True)

    async def on_user_spoken_audit(self, event: UserSpokenEvent) -> None:
        """审计日志。生产环境这里换成写审计表 / 推 Kafka。"""
        logger.info(
            "[AUDIT] sessionId=%s userId=%s utterance=%s",
            event.session_id,
            event.user_id,
            event.utterance,
        )


@dataclass
class RecordingListener:
    """测试用监听者：把收到的事件记下来。"""

    received: list[UserSpokenEvent] = field(default_factory=list)

    async def __call__(self, event: UserSpokenEvent) -> None:
        self.received.append(event)
