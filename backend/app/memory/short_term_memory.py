"""会话短期记忆：Redis List + TTL 保存最近几轮对话的精简快照。"""

import json

from pydantic import BaseModel
from redis.asyncio import Redis

from app.config import get_settings


class Turn(BaseModel):
    """一轮对话。"""

    role: str
    agent: str | None = None
    text: str
    ts: int


class ShortTermMemory:
    """短期记忆存储。

    - Key：``voice_agent:short_memory:{sessionId}``，Redis List。
    - 写入：``RPUSH`` 追加 + ``LTRIM`` 只保留最近 ``max_turns`` 条 + 刷新 TTL。
    - 读取：``LRANGE key -limit -1`` 取最近 ``limit`` 条，保持时间正序。
    """

    KEY_PREFIX = "voice_agent:short_memory:"

    def __init__(
        self,
        redis: Redis,
        max_turns: int | None = None,
        ttl_minutes: int | None = None,
    ) -> None:
        settings = get_settings()
        self._redis = redis
        self._max_turns = max_turns or settings.session_max_history_turns
        self._ttl_minutes = ttl_minutes or settings.session_ttl_minutes

    def _key(self, session_id: str) -> str:
        return f"{self.KEY_PREFIX}{session_id}"

    async def append(self, session_id: str, turn: Turn) -> None:
        """追加一轮对话，并裁剪到最近 ``max_turns`` 条、刷新过期时间。"""
        key = self._key(session_id)
        await self._redis.rpush(key, json.dumps(turn.model_dump(), ensure_ascii=False))
        await self._redis.ltrim(key, -self._max_turns, -1)
        await self._redis.expire(key, self._ttl_minutes * 60)

    async def recent(self, session_id: str, limit: int) -> list[Turn]:
        """读取最近 ``limit`` 轮对话（时间正序）。"""
        raws = await self._redis.lrange(self._key(session_id), -limit, -1)
        if not raws:
            return []
        return [Turn(**json.loads(raw)) for raw in raws]

    async def clear(self, session_id: str) -> None:
        await self._redis.delete(self._key(session_id))
