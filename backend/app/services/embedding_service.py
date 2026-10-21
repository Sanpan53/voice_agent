"""Embedding 服务：把文本向量化。

使用 dashscope SDK 的 ``TextEmbedding`` 调用 DashScope 的 embedding 端点。
网络调用放入线程池，避免阻塞事件循环。
"""

import asyncio
from abc import ABC, abstractmethod

from dashscope import TextEmbedding

from app.config import get_settings


class EmbeddingError(RuntimeError):
    """向量化失败。"""


class EmbeddingService(ABC):
    """Embedding 抽象接口，便于测试时用假实现替换。

    两个入口，**语义不同、缓存策略也不同**：

    * :meth:`embed` —— 要建索引的文本（商品文案、FAQ 问题）。内容是固定的，
      算一次能用很久，**允许走缓存**。
    * :meth:`embed_query` —— 用户这一句话。每次都不一样，缓存命中率接近零，
      **不该走缓存**（存进去只是白占内存）。

    把两个入口做进接口，是为了让"这条路径不该被缓存"变成**签名上的事实**，
    而不是靠某个人记得别给某个调用点加缓存。
    """

    @abstractmethod
    async def embed(self, text: str) -> list[float]:
        """把单段文本转为向量（可缓存路径）。"""

    async def embed_query(self, text: str) -> list[float]:
        """把用户 query 转为向量（**不走缓存**路径）。

        基类默认直接委托 :meth:`embed`——对"本来就没有缓存"的实现来说
        （比如 :class:`DashScopeEmbeddingService` 自己、以及测试里的各种假实现），
        两个入口的行为完全一样。只有 :class:`~app.services.embedding_cache.CachedEmbeddingService`
        会覆写它来绕过缓存。
        """
        return await self.embed(text)


class DashScopeEmbeddingService(EmbeddingService):
    """DashScope text-embedding-v3 实现。"""

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        dimension: int | None = None,
    ) -> None:
        settings = get_settings()
        self._model = model or settings.embedding_model
        self._api_key = api_key or settings.dashscope_api_key
        self._dimension = dimension or settings.embedding_dim

    async def embed(self, text: str) -> list[float]:
        resp = await asyncio.to_thread(
            TextEmbedding.call,
            model=self._model,
            input=text,
            api_key=self._api_key,
            dimension=self._dimension,
        )
        if resp.status_code != 200:
            raise EmbeddingError(
                f"embedding 请求失败: code={resp.status_code}, message={resp.message}"
            )
        try:
            vector = resp.output["embeddings"][0]["embedding"]
        except (KeyError, IndexError, TypeError) as exc:
            raise EmbeddingError(f"embedding 响应缺少向量: {resp.output}") from exc

        if len(vector) != self._dimension:
            raise EmbeddingError(
                f"向量维度不匹配: 期望 {self._dimension}，实际 {len(vector)}"
            )
        return vector
