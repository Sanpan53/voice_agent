"""索引重建服务：批量为商品 / FAQ 生成并回写向量。"""

from pydantic import ConfigDict
from pydantic.dataclasses import dataclass

from app.repositories.faq_repository import FaqRepository
from app.repositories.product_repository import ProductRepository
from app.services.embedding_service import EmbeddingService
from app.services.product_text_builder import build


@dataclass(config=ConfigDict(frozen=True))
class IndexResult:
    """重建结果统计（同时作为 API 响应模型）。"""

    total: int
    indexed: int
    failed: int


class ProductIndexService:
    """为在售商品生成向量。"""

    def __init__(
        self,
        repository: ProductRepository,
        embedding: EmbeddingService,
    ) -> None:
        self._repository = repository
        self._embedding = embedding

    async def reindex(self) -> IndexResult:
        products = await self._repository.find_by_status("ON_SALE")
        indexed = 0
        failed = 0
        for product in products:
            try:
                text = build(
                    name=product.name,
                    brand=product.brand,
                    category_l2=product.category_l2,
                    selling_points=product.selling_points,
                    description=product.description,
                    attributes=product.attributes,
                )
                vector = await self._embedding.embed(text)
                await self._repository.update_embedding(product.id, vector)
                indexed += 1
            except Exception:
                failed += 1
        return IndexResult(total=len(products), indexed=indexed, failed=failed)


class FaqIndexService:
    """为已发布 FAQ 生成向量。"""

    def __init__(
        self,
        repository: FaqRepository,
        embedding: EmbeddingService,
    ) -> None:
        self._repository = repository
        self._embedding = embedding

    async def reindex(self) -> IndexResult:
        entries = await self._repository.find_published()
        indexed = 0
        failed = 0
        for entry in entries:
            try:
                vector = await self._embedding.embed(entry.question)
                await self._repository.update_embedding(entry.id, vector)
                indexed += 1
            except Exception:
                failed += 1
        return IndexResult(total=len(entries), indexed=indexed, failed=failed)
