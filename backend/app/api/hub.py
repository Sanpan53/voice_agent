"""MsgHub 调试接口。

只用于本地调试 / 手工验证多视角点评团，不参与正式业务流程。
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.api.dependencies import get_perspective_hub_service, get_recommend_orchestrator
from app.schemas.recommend import RecommendResult
from app.services.perspective_hub_service import PerspectiveHubService
from app.services.recommend_orchestrator import RecommendOrchestrator

router = APIRouter(prefix="/api/v1/hub", tags=["hub-debug"])


class PerspectiveDebugRequest(BaseModel):
    """``/api/v1/hub/perspective`` 的请求体。"""

    utterance: str = ""
    slots: dict[str, Any] = Field(default_factory=dict)


class PerspectiveDebugResponse(BaseModel):
    """先是推荐结果，再是点评纪要。"""

    recommendation: RecommendResult
    perspective_digest: str = Field(alias="perspectiveDigest")

    model_config = {"populate_by_name": True}


@router.post("/perspective", response_model=PerspectiveDebugResponse)
async def perspective(
    orchestrator: Annotated[RecommendOrchestrator, Depends(get_recommend_orchestrator)],
    hub: Annotated[PerspectiveHubService, Depends(get_perspective_hub_service)],
    session_id: Annotated[str, Query(alias="sessionId")],
    user_id: Annotated[int, Query(alias="userId")],
    body: PerspectiveDebugRequest,
) -> PerspectiveDebugResponse:
    """先跑推荐链路拿 Top3，再把 Top3 交给点评团开一场讨论。

    这里要验证的是"MsgHub 多视角讨论"能跑起来，**推荐环节是不是并行的不重要**；
    想换成并行版把 ``get_recommend_orchestrator`` 依赖换成
    :class:`~app.services.parallel_recommend_service.ParallelRecommendService` 即可。
    """
    rec = await orchestrator.recommend(session_id, user_id, body.utterance, body.slots)
    digest = await hub.discuss(session_id, body.utterance, rec.items)
    return PerspectiveDebugResponse(recommendation=rec, perspectiveDigest=digest)
