"""Embedding 的 Redis 缓存装饰器。

为 ``EmbeddingService.embed`` 配一层缓存，TTL 单独设为 7 天。

**先说清楚这一层兜的是什么。** 商品向量其实早就缓存过了——不在 Redis，
在 **PG 的 ``embedding`` 列**里：索引搭建时算一次写进去，之后检索直接读列，
根本不重算。所以这层 Redis 缓存兜的是**长尾**：FAQ 重建索引、同一段文案在
一次重建里被重复请求、以及将来新增的"临时算一下"的调用点。别指望它带来数量级
的降幅——真正的大头是"压根不重算"，那件事索引模块已经做了。

.. important::

   **用户 query 不走这条路。** 每次说的都不一样，缓存命中率接近零，
   存进去只是白占内存（一条 1024 维向量按 JSON 存大约 20 KB）。
   所以 :meth:`embed_query` 在这里是**直接穿透**到底层实现，一个字都不缓存。

这一层用**装饰器模式**包一层，不让缓存逻辑对调用方完全透明——好处是装配时
看得见（``get_embedding_service()`` 里明明白白写着 ``CachedEmbeddingService(...)``），
坏处是**每个调用点都得用对方法名**。
"""

import logging

from app.services.cache_service import RedisCacheService
from app.services.embedding_service import EmbeddingService
from app.services.utterance_fingerprint import compute as compute_fingerprint

logger = logging.getLogger(__name__)

#: 缓存有效期：7 天。
DEFAULT_TTL_SECONDS = 7 * 24 * 3600


class CachedEmbeddingService(EmbeddingService):
    """给另一个 :class:`EmbeddingService` 加一层 Redis 缓存。

    Args:
        delegate: 真正干活的实现（一般是 :class:`DashScopeEmbeddingService`）。
        cache: Redis JSON 缓存。**建议单独给一个前缀**（``voice_agent:embed:``），
            别和业务缓存混在一个命名空间里——两者 TTL 差 300 倍。
        ttl_seconds: 缓存有效期，默认 7 天。

    Note:
        **缓存故障绝不冒泡。** 读写 Redis 出任何问题都降级成"未命中 / 不写回"，
        继续走真实的 embedding 调用。理由和 TTS 那段一样：
        缓存是省钱的手段，不是功能的一部分——它挂掉最多让这一轮贵一点，
        绝不能让它把一次正常的商品检索打成 500。
    """

    def __init__(
        self,
        delegate: EmbeddingService,
        cache: RedisCacheService,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._delegate = delegate
        self._cache = cache
        self._ttl_seconds = ttl_seconds

    @staticmethod
    def build_key(text: str) -> str:
        """缓存 key：文本指纹。

        **用指纹而不是原文**——原文当 key 又长又占内存，还有非 ASCII 和换行的转义问题。
        指纹的归一化规则见 :mod:`app.services.utterance_fingerprint`，
        所以"跑鞋推荐"和"跑鞋推荐。"共用同一条缓存，与意图缓存口径一致。
        """
        return compute_fingerprint(text)

    async def embed(self, text: str) -> list[float]:
        """查缓存 → 未命中再算 → 写回。"""
        key = self.build_key(text)
        try:
            cached = await self._cache.get(key)
        except Exception:  # noqa: BLE001 — 缓存读失败当作未命中
            logger.warning(
                "[Cost] Embedding 缓存读取失败，降级为直接计算 key=%s", key, exc_info=True
            )
            cached = None

        if isinstance(cached, list) and cached:
            logger.debug("[Cost] Embedding 命中缓存 key=%s", key)
            return [float(value) for value in cached]

        vector = await self._delegate.embed(text)

        try:
            await self._cache.put(key, vector, ttl_seconds=self._ttl_seconds)
        except Exception:  # noqa: BLE001 — 写失败只影响下一次，不影响这一次
            logger.warning("[Cost] Embedding 缓存写入失败 key=%s", key, exc_info=True)
        return vector

    async def embed_query(self, text: str) -> list[float]:
        """用户 query：**直接穿透**，不读也不写缓存。

        调用方（:class:`~app.services.product_search_service.PgVectorProductSearchService`）
        用的是这个方法，所以用户 query 这条路从**签名上**就不可能走到缓存里，
        不需要在 key 里塞条件、也不需要靠人记得别加缓存。
        """
        return await self._delegate.embed_query(text)
