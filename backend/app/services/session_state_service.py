"""会话状态机服务：PG 与 Redis 双写。

**PG 是真源，Redis 只是缓存。** 这条原则已经落到了代码里：
写入顺序先 PG 后 Redis，Redis 读写失败一律吞掉、不回滚 PG。
详见 :meth:`SessionStateService.save` 的说明。
"""

import json
import logging

from redis.asyncio import Redis

from app.config import get_settings
from app.models.session import SessionState
from app.repositories.session_repository import SessionStateRepository

logger = logging.getLogger(__name__)

# 会话状态的初始阶段。
INITIAL_PHASE = "INTENT"


class SessionStateService:
    """会话状态的 Redis + PG 读写。

    **读优先走 Redis，写同时写 PG 和 Redis**：崩了恢复、重启不丢，又足够快。
    Redis 过期了从 PG 恢复，不会丢会话。
    """

    KEY_PREFIX = "voice_agent:session:"

    def __init__(
        self,
        repo: SessionStateRepository,
        redis: Redis,
        ttl_minutes: int | None = None,
    ) -> None:
        self._repo = repo
        self._redis = redis
        self._ttl_minutes = ttl_minutes or get_settings().session_ttl_minutes

    def _key(self, session_id: str) -> str:
        return f"{self.KEY_PREFIX}{session_id}"

    async def load(self, session_id: str) -> SessionState:
        """读取会话状态：Redis 未命中则回源 PG，PG 也没有则返回初始状态（不落库）。

        **Redis 读失败不能当成"没有缓存"**——那会把会话状态悄悄重置回初始态，
        用户正聊到一半突然"失忆"。Redis 挂了就直接回源 PG，PG 才是真源。
        """
        try:
            cached = await self._redis.get(self._key(session_id))
        except Exception:  # noqa: BLE001 — 缓存读失败退化为回源 PG
            logger.warning(
                "Redis 读取会话状态失败，回源 PG sessionId=%s", session_id, exc_info=True
            )
        else:
            if cached is not None:
                return self._from_json(cached)

        from_db = await self._repo.find_by_id(session_id)
        if from_db is not None:
            await self._sync_to_redis(from_db)
            return from_db

        return SessionState(session_id=session_id, phase=INITIAL_PHASE)

    async def save(self, state: SessionState) -> None:
        """写 PG 并同步 Redis。

        **PG 是真源，Redis 只是缓存**：

        + 写入顺序**先 PG 再 Redis**；
        + Redis 写失败**不回滚 PG**——否则 Redis 一抖动整个业务就不可用；
        + 下次 :meth:`load` 时 Redis 未命中会自动从 PG 重建。

        让 Redis 异常不影响 PG 写入：**吞掉 Redis 异常**——PG 的写入早已完成，
        此时再往外抛只会让调用方以为整个保存都失败了。
        """
        await self._repo.save(state)
        await self._sync_to_redis(state)

    async def _sync_to_redis(self, state: SessionState) -> None:
        """同步到 Redis；失败只记 warn，不影响 PG 已经落库的事实。"""
        try:
            await self._redis.set(
                self._key(state.session_id),
                self._to_json(state),
                ex=self._ttl_minutes * 60,
            )
        except Exception:  # noqa: BLE001 — 缓存写失败不回滚真源
            logger.warning(
                "Redis 同步失败，下次 load 会从 PG 重建 sessionId=%s",
                state.session_id,
                exc_info=True,
            )

    @staticmethod
    def _to_json(state: SessionState) -> str:
        payload = {
            "sessionId": state.session_id,
            "phase": state.phase,
            "currentIntent": state.current_intent,
            "slots": state.slots or {},
            "pendingAsk": state.pending_ask,
            "lastRecommendations": list(state.last_recommendations or []),
        }
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _from_json(raw: str) -> SessionState:
        data = json.loads(raw)
        return SessionState(
            session_id=data["sessionId"],
            phase=data["phase"],
            current_intent=data.get("currentIntent"),
            slots=data.get("slots") or {},
            pending_ask=data.get("pendingAsk"),
            last_recommendations=list(data.get("lastRecommendations") or []),
        )
