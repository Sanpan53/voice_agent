"""商品语义检索接口。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_product_search_service
from app.schemas.product import ProductOut
from app.services.product_search_service import ProductSearchService

router = APIRouter()


@router.get("/api/v1/search", response_model=list[ProductOut], tags=["search"])
async def search(
    q: Annotated[str, Query(min_length=1, max_length=200)],
    budget: Annotated[float | None, Query(ge=0)] = None,
    service: Annotated[ProductSearchService, Depends(get_product_search_service)] = ...,
) -> list[ProductOut]:
    """按语义检索商品；``budget`` 作为最高价过滤条件参与混合检索。"""
    filters = {"max_price": budget} if budget is not None else None
    return await service.search(q, top_k=5, filters=filters)
