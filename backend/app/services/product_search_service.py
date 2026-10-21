"""商品检索服务：查询向量化 -> 混合检索 -> DTO 映射。"""

from abc import ABC, abstractmethod
from typing import Any

from app.repositories.product_repository import ProductRepository
from app.schemas.product import ProductOut
from app.services.embedding_service import EmbeddingService


class ProductSearchService(ABC):
    """商品检索抽象接口，便于测试替换。"""

    @abstractmethod
    async def search(
        self,
        query: str,
        top_k: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[ProductOut]:
        """语义检索商品。"""


class PgVectorProductSearchService(ProductSearchService):
    """基于 pgvector 的语义检索实现。"""

    def __init__(
        self,
        repository: ProductRepository,
        embedding: EmbeddingService,
    ) -> None:
        self._repository = repository
        self._embedding = embedding

    async def search(
        self,
        query: str,
        top_k: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[ProductOut]:
        # 用 embed_query 而不是 embed：用户这一句话不该进 embedding 缓存
        # 这条路径每次内容都不同，缓存命中率接近零，只会白占内存。
        vector = await self._embedding.embed_query(query)
        products = await self._repository.hybrid_search(vector, filters=filters, top_k=top_k)
        return [ProductOut.model_validate(p, from_attributes=True) for p in products]
