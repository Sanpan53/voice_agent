"""管理员重建索引接口。"""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.dependencies import get_faq_index_service, get_product_index_service
from app.services.index_service import FaqIndexService, IndexResult, ProductIndexService

router = APIRouter()


@router.post("/api/v1/admin/reindex", response_model=IndexResult, tags=["admin"])
async def reindex(
    service: Annotated[ProductIndexService, Depends(get_product_index_service)],
) -> IndexResult:
    """为全部在售商品重建向量。"""
    return await service.reindex()


@router.post("/api/v1/admin/faq-reindex", response_model=IndexResult, tags=["admin"])
async def faq_reindex(
    service: Annotated[FaqIndexService, Depends(get_faq_index_service)],
) -> IndexResult:
    """为全部已发布 FAQ 重建向量。"""
    return await service.reindex()
