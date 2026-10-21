"""意图识别结果缓存。

同一用户短时间内反复说相似的话（比如网络抖动、用户复述），每次都调 LLM 太浪费。
用一个指纹级缓存挡住：key = sessionId + 语句哈希，TTL 5 分钟。

**缓存 key 用 sessionId 而不是 userId**——同一会话内重复 / 复述命中率高，跨会话的
指纹相同概率低；用 sessionId 也能天然跟 ``ShortTermMemory`` / ``AgentFactory`` 的
生命周期对齐，会话结束不留脏数据。
"""

from redis.asyncio import Redis

from app.schemas.intent import IntentResult
from app.services.utterance_fingerprint import compute as compute_fingerprint

#: Key 前缀
KEY_PREFIX = "voice_agent:intent_cache:"

#: 缓存有效期：5 分钟
TTL_SECONDS = 300


def fingerprint(utterance: str) -> str:
    """语句指纹。**归一化后再哈希**。

    原先这里只做 ``strip()``，于是"想买双跑鞋"和"想买双跑鞋。"拿到两个
    不同的 key、命中不了同一个缓存条目——用户多打一个句号就多烧一次 Intent LLM。
    现在委托给 :func:`~app.services.utterance_fingerprint.compute`，按统一规则
    去标点、去空白、转小写，再取 md5 前 16 位。

    这里用稳定且抗碰撞的 md5 摘要，而不是 Python 内置的 ``hash()``——后者带
    进程随机化（PYTHONHASHSEED），跨进程不稳定，**不能拿来当缓存 key**
    （重启即全量失效，且不报错）。

    Note:
        ``IntentCache`` 内部只认这个函数，**调用方（``IntentService``）对 key 的实现
        无感知**——这是把指纹策略封在缓存层、没散进 Service 的收益。
    """
    return compute_fingerprint(utterance)


class IntentCache:
    """意图结果缓存。

    Args:
        redis: 异步 Redis 客户端。
        ttl_seconds: 缓存有效期，默认 300 秒。
    """

    def __init__(self, redis: Redis, ttl_seconds: int = TTL_SECONDS) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds

    @staticmethod
    def build_key(session_id: str, utterance: str) -> str:
        return f"{KEY_PREFIX}{session_id}:{fingerprint(utterance)}"

    async def get(self, session_id: str, utterance: str) -> IntentResult | None:
        """读缓存。反序列化失败一律当作未命中，不让脏数据打穿业务层。"""
        raw = await self._redis.get(self.build_key(session_id, utterance))
        if raw is None:
            return None
        try:
            return IntentResult.model_validate_json(raw)
        except Exception:  # noqa: BLE001 — 任何脏数据都降级为未命中
            return None

    async def put(self, session_id: str, utterance: str, result: IntentResult) -> None:
        """写缓存。写失败不影响主流程。"""
        try:
            await self._redis.set(
                self.build_key(session_id, utterance),
                result.model_dump_json(),
                ex=self._ttl_seconds,
            )
        except Exception:  # noqa: BLE001
            pass
