"""Redis 异步客户端（redis-py asyncio）。"""

import redis.asyncio as aioredis

from app.config import get_settings

_client: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    """惰性创建 Redis 客户端（from_url 不立即建立连接）。"""
    global _client
    if _client is None:
        _client = aioredis.from_url(get_settings().redis_url, decode_responses=True)
    return _client


async def close_redis() -> None:
    """关闭已经创建的 Redis 客户端；未创建时不做任何操作。"""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
