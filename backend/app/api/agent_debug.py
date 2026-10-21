"""Agent 调试接口。

这些接口只用于本地调试 / 手工验证 Agent 行为，不参与正式业务流程。
"""

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query
from pydantic import BaseModel, Field

from app.api.dependencies import (
    get_clarify_service,
    get_emotion_service,
    get_intent_service,
    get_recommend_orchestrator,
)
from app.schemas.clarify import ClarifyResult
from app.schemas.emotion import EmotionResult
from app.schemas.intent import IntentResult
from app.schemas.recommend import RecommendResult
from app.services.clarify_service import ClarifyService
from app.services.emotion_service import EmotionService
from app.services.intent_service import IntentService
from app.services.recommend_orchestrator import RecommendOrchestrator

router = APIRouter(prefix="/api/v1/agent", tags=["agent-debug"])


class ClarifyDebugRequest(BaseModel):
    """``/api/v1/agent/clarify`` 的请求体。"""

    utterance: str
    slots: dict[str, Any] = Field(default_factory=dict)


class RecommendDebugRequest(BaseModel):
    """``/api/v1/agent/recommend`` 的请求体。"""

    utterance: str
    slots: dict[str, Any] = Field(default_factory=dict)


class EmotionDebugRequest(BaseModel):
    """``/api/v1/agent/emotion`` 的请求体。

    直接吃 ``/recommend`` 的返回体，所以 ``rec`` 复用
    :class:`~app.schemas.recommend.RecommendResult`——把推荐接口的输出原样贴进来就能
    调试口播效果。

    接口加了 ``userNeeds``：情感 Agent 现在要自己编理由，光有商品属性不够，
    还得知道用户在找什么。没传就是空串，不报错。
    """

    utterance: str
    user_needs: str = ""
    rec: RecommendResult


@router.post("/intent", response_model=IntentResult)
async def classify_intent(
    service: Annotated[IntentService, Depends(get_intent_service)],
    session_id: Annotated[str, Query(alias="sessionId")],
    utterance: Annotated[str, Body(media_type="text/plain")],
) -> IntentResult:
    """意图分类与槽位抽取。

    ``POST /api/v1/agent/intent?sessionId=...``：sessionId 走
    query 参数，用户原话走**纯文本 body**（不是 JSON）。
    """
    return await service.classify(session_id, utterance)


@router.post("/clarify", response_model=ClarifyResult)
async def decide_clarify(
    service: Annotated[ClarifyService, Depends(get_clarify_service)],
    session_id: Annotated[str, Query(alias="sessionId")],
    request: ClarifyDebugRequest,
) -> ClarifyResult:
    """判断该追问还是可以直接推荐。

    ``POST /api/v1/agent/clarify?sessionId=...``：sessionId 走
    query 参数，``{utterance, slots}`` 走 JSON body。
    """
    return await service.decide(session_id, request.utterance, request.slots)


@router.post("/recommend", response_model=RecommendResult)
async def recommend_products(
    orchestrator: Annotated[RecommendOrchestrator, Depends(get_recommend_orchestrator)],
    session_id: Annotated[str, Query(alias="sessionId")],
    user_id: Annotated[int, Query(alias="userId")],
    request: RecommendDebugRequest,
) -> RecommendResult:
    """跑完整条推荐链路（检索 → 加权 → Rerank → 生成理由）。

    ``POST /api/v1/agent/recommend?sessionId=...&userId=...``：sessionId 与
    userId 都走 query 参数。

    Note:
        有一个常被当成 Bug 的现象：``items`` 返回空数组时先别怀疑检索，
        核对一下商品库里该品类的价格区间和 ``attributes.gender`` 取值。例如跑鞋最低价
        599、``gender`` 只有 ``unisex`` / ``female``，那么 ``budget=500`` 或
        ``gender=male`` 必然是空。这时会依次放宽预算 +30%、丢掉品类，三步都空才返回
        ``explanationTone=empty``——这是**设计行为**。
    """
    return await orchestrator.recommend(session_id, user_id, request.utterance, request.slots)


@router.post("/emotion", response_model=EmotionResult)
async def wrap_emotion(
    service: Annotated[EmotionService, Depends(get_emotion_service)],
    session_id: Annotated[str, Query(alias="sessionId")],
    request: EmotionDebugRequest,
) -> EmotionResult:
    """把推荐结果包装成口语回复（供 TTS 合成）。

    ``POST /api/v1/agent/emotion?sessionId=...``：``rec`` 字段直接
    接受 :class:`~app.schemas.recommend.RecommendResult` 的 JSON 形态——把
    ``/recommend`` 的返回体贴进来即可。

    注意 ``rec.items[].reason`` 现在**不参与生成**（情感 Agent 自己编），
    贴进来是空的也没关系；真正起作用的是 ``attributes``。
    """
    return await service.wrap(
        session_id, request.utterance, request.user_needs, request.rec
    )
