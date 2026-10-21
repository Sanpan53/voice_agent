"""FAQ 问答接口。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_faq_repository, get_faq_vector_service
from app.repositories.faq_repository import FaqRepository
from app.services.faq_vector_service import FaqVectorService

router = APIRouter()


@router.get("/api/v1/faq/ask", tags=["faq"])
async def ask(
    q: Annotated[str, Query(min_length=1, max_length=512)],
    merchant_id: Annotated[int, Query(ge=0, alias="merchantId")] = 0,
    service: Annotated[FaqVectorService, Depends(get_faq_vector_service)] = ...,
    repository: Annotated[FaqRepository, Depends(get_faq_repository)] = ...,
) -> dict[str, object]:
    """FAQ 语义问答：命中返回答案，未命中返回 hit=false。"""
    match = await service.search_best(q, merchant_id)
    if match is None:
        return {"hit": False, "question": None, "answer": None, "category": None}

    entry = await repository.find_by_id(match.faq_id)
    await repository.increment_hit_count(match.faq_id)
    return {
        "hit": True,
        "question": entry.question,
        "answer": entry.answer,
        "category": entry.category,
    }
