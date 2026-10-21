"""Redis 缓存服务。

采用字符串 Key + JSON Value + TTL 的协议。键前缀默认 ``voice_agent:cache:``。
"""

import json
from typing import Any

from pydantic import BaseModel
from redis.asyncio import Redis


class RedisCacheService:
    """Redis JSON 缓存，字符串 Key + JSON Value。"""

    def __init__(
        self,
        redis: Redis,
        key_prefix: str = "voice_agent:cache:",
        default_ttl_seconds: int = 1800,
    ) -> None:
        self.redis = redis
        self.key_prefix = key_prefix
        self.default_ttl_seconds = default_ttl_seconds

    async def get(self, key: str) -> Any | None:
        raw_value = await self.redis.get(self.key_prefix + key)
        return None if raw_value is None else json.loads(raw_value)

    async def put(
        self,
        key: str,
        value: Any,
        ttl_seconds: int | None = None,
    ) -> None:
        payload = (
            value.model_dump(mode="json")
            if isinstance(value, BaseModel)
            else value
        )
        await self.redis.set(
            self.key_prefix + key,
            json.dumps(payload, ensure_ascii=False),
            ex=ttl_seconds or self.default_ttl_seconds,
        )

    async def delete(self, key: str) -> None:
        await self.redis.delete(self.key_prefix + key)
