"""会话范围的最薄缓存。

``SessionScope`` 不是业务对象——它是**会话级别的旁路数据**，只在推荐链路上被读，
没必要进 PG。所以放 Redis，key 前缀 ``voice_agent:scope:{sessionId}``，
与 ``voice_agent:session:{id}``（会话状态机）同生命周期、同一个 TTL。

> 📌 注意区分这两个 key：
> + ``voice_agent:session:{id}`` → 会话**状态机**（phase / slots），决定下一步做什么；
> + ``voice_agent:scope:{id}`` → 会话**可见范围**（允许哪些商家），决定能看什么。
>
> 前者会随会话推进不断被改写，后者**一次写入、全程不变**——入口决定了范围，
> 聊到一半不会换。
"""

import json
import logging

from pydantic import ValidationError
from redis.asyncio import Redis

from app.config import get_settings
from app.schemas.session import SessionScope

logger = logging.getLogger(__name__)

#: Redis key 前缀，后接 ``sessionId``。
SCOPE_KEY_PREFIX = "voice_agent:scope:"


class SessionScopeCache:
    """``SessionScope`` 的读写。

    Args:
        redis: Redis 客户端。
        ttl_minutes: 过期时间，默认跟会话 TTL（30 分钟）走。
    """

    def __init__(self, redis: Redis, ttl_minutes: int | None = None) -> None:
        self._redis = redis
        self._ttl_minutes = ttl_minutes or get_settings().session_ttl_minutes

    def _key(self, session_id: str) -> str:
        return f"{SCOPE_KEY_PREFIX}{session_id}"

    async def put(self, session_id: str, scope: SessionScope) -> None:
        """写入范围，并刷新 TTL。"""
        await self._redis.set(
            self._key(session_id),
            scope.model_dump_json(by_alias=True),
            ex=self._ttl_minutes * 60,
        )

    async def get(self, session_id: str) -> SessionScope | None:
        """读取范围；不存在或解析失败都返回 ``None``。

        **为什么要吞**：调用方（推荐链路）拿到 ``None`` 会降级成全平台，
        而"降级成全平台"在权限语义上是**放宽**——这里必须谨慎：见
        :meth:`~app.services.recommend_orchestrator.RecommendOrchestrator.recommend`
        的说明，降级方向是有意选的。
        """
        raw = await self._redis.get(self._key(session_id))
        if raw is None:
            return None
        try:
            return SessionScope.model_validate_json(raw)
        except ValidationError as exc:
            logger.warning("会话范围解析失败 sessionId=%s: %s", session_id, exc)
            return None

    async def clear(self, session_id: str) -> None:
        await self._redis.delete(self._key(session_id))


def scope_to_json(scope: SessionScope) -> str:
    """便于测试/调试的序列化出口（与 Redis 里存的内容一致）。"""
    return json.dumps(json.loads(scope.model_dump_json(by_alias=True)), ensure_ascii=False)
