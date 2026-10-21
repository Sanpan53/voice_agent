"""用户画像与会话记忆调试接口。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Path

from app.api.dependencies import get_short_term_memory, get_user_profile_service
from app.memory.short_term_memory import ShortTermMemory, Turn
from app.schemas.profile import UserProfileSnapshot
from app.services.user_profile_service import UserProfileService

router = APIRouter()


@router.get(
    "/api/v1/profile/memory/{session_id}",
    response_model=list[Turn],
    tags=["profile"],
)
async def get_memory(
    session_id: Annotated[str, Path(min_length=1, max_length=64)],
    memory: Annotated[ShortTermMemory, Depends(get_short_term_memory)],
) -> list[Turn]:
    """读取会话最近 10 轮短期记忆。

    注意：该路由必须注册在 ``/{user_id}`` 之前，否则 ``memory`` 会被当成
    用户 ID 匹配掉。
    """
    return await memory.recent(session_id, 10)


@router.get(
    "/api/v1/profile/{user_id}",
    response_model=UserProfileSnapshot,
    tags=["profile"],
)
async def get_profile(
    user_id: Annotated[int, Path(gt=0)],
    service: Annotated[UserProfileService, Depends(get_user_profile_service)],
) -> UserProfileSnapshot:
    """读取合成画像快照（静态画像 + 动态画像）。"""
    return await service.load(user_id)
