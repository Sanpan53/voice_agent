"""会话 TTL 过期监听。

第三个回流触发点。前两个是**主动**的：

1. WebSocket 关闭；
2. 订单确认完成。

但用户既不手动关页面也不下单呢？——放任 30 分钟后 Redis ``voice_agent:session:{id}``
自动过期。这种**静默离开**同样要触发回流，否则这部分用户的行为全丢了。

直接用 redis-py 的 pubsub 订阅 ``__keyevent@<db>__:expired`` 频道。

.. warning::

   Redis 的 keyspace notifications **默认是关闭的**，必须显式开启::

       # 本地临时开（重启失效）
       redis-cli CONFIG SET notify-keyspace-events Ex

       # 生产：写进 redis.conf 持久化
       notify-keyspace-events Ex

   其中 ``E`` = keyevent 事件，``x`` = 过期事件。没开的话监听器**安静地收不到
   任何消息**，不报错——这是排查这类问题最费时间的地方。
"""

import contextlib
import logging
from collections.abc import Awaitable, Callable

from redis.asyncio import Redis

from app.services.long_term_memory_writer import LongTermMemoryWriter
from app.services.session_service import SessionService

logger = logging.getLogger(__name__)

#: 会话状态在 Redis 里的键前缀（与 ``SessionStateService.KEY_PREFIX`` 一致）
SESSION_KEY_PREFIX = "voice_agent:session:"

#: 过期事件频道的模板：``__keyevent@0__:expired``
EXPIRED_CHANNEL_TEMPLATE = "__keyevent@{}__:expired"

#: 回流回调类型（便于测试注入假实现）
FlushCallback = Callable[[str, int], Awaitable[bool]]


def parse_expired_session(expired_key: str) -> str | None:
    """从过期的键名里解析出 sessionId；不是会话键则返回 ``None``。

    过滤规则很简单：过期事件频道是**全局的**，库里的每个键过期都会推过来，
    不过滤的话会拿一堆无关的键去查会话表。
    """
    if not expired_key.startswith(SESSION_KEY_PREFIX):
        return None
    session_id = expired_key[len(SESSION_KEY_PREFIX) :]
    return session_id or None


class SessionExpireListener:
    """订阅 Redis 过期事件，把静默离开的会话也回流一次。

    Args:
        redis: Redis 客户端（用来拿 pubsub 和数据库编号）。
        writer: 长期记忆回流器。
        session_service: 会话主表服务（**从 PG 查 userId**——
            Redis 里的会话状态已经过期了，只能回源）。
    """

    def __init__(
        self,
        redis: Redis,
        writer: LongTermMemoryWriter,
        session_service: SessionService,
    ) -> None:
        self._redis = redis
        self._writer = writer
        self._session_service = session_service
        self._pubsub = None
        self._task = None

    @property
    def channel(self) -> str:
        """当前 Redis 数据库对应的过期事件频道。"""
        db = self._redis.connection_pool.connection_kwargs.get("db", 0)
        return EXPIRED_CHANNEL_TEMPLATE.format(db)

    async def handle_expired_key(self, expired_key: str) -> bool:
        """处理一个过期键。这是本类的核心逻辑，也是测试的主要入口。

        Returns:
            是否真的触发了回流。
        """
        session_id = parse_expired_session(expired_key)
        if session_id is None:
            return False

        # userId 从 PG 的 session 主表查——Redis 已过期拿不到
        user_id = await self._session_service.find_user_id(session_id)
        if user_id is None:
            logger.warning(
                "[Listener] 会话已过期但 PG 查不到 userId，跳过 sessionId=%s", session_id
            )
            return False

        logger.info(
            "[Listener] session expired sessionId=%s userId=%s, flush memory",
            session_id,
            user_id,
        )
        await self._writer.flush_on_session_end(session_id, user_id)
        return True

    async def start(self) -> None:
        """订阅频道并在后台消费。**进程启动时调一次。**"""
        import asyncio

        self._pubsub = self._redis.pubsub()
        await self._pubsub.subscribe(self.channel)
        self._task = asyncio.create_task(self._consume())
        logger.info("[Listener] 已订阅会话过期事件 channel=%s", self.channel)

    async def _consume(self) -> None:
        """消费循环。单条消息处理失败不能中断整个订阅。"""
        async for message in self._pubsub.listen():
            if message.get("type") != "message":
                continue
            raw = message.get("data")
            key = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            try:
                await self.handle_expired_key(key)
            except Exception:  # noqa: BLE001 — 一条失败不能拖垮监听
                logger.error("[Listener] 处理过期键失败 key=%s", key, exc_info=True)

    async def stop(self) -> None:
        """取消订阅并停掉后台任务。"""
        import asyncio

        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._pubsub is not None:
            with contextlib.suppress(Exception):
                await self._pubsub.unsubscribe(self.channel)
                await self._pubsub.aclose()
            self._pubsub = None
