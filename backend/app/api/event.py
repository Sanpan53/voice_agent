"""事件广播调试接口。

只用于本地验证"非 Agent 监听者"这条旁路，不参与正式业务流程。
"""

import time
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.api.dependencies import get_voice_event_publisher
from app.events.voice_events import VoiceEventPublisher

router = APIRouter(prefix="/api/v1/event", tags=["event-debug"])


class UserSpokenRequest(BaseModel):
    """``/api/v1/event/user-spoken`` 的请求体。"""

    utterance: str = ""


@router.post("/user-spoken")
async def user_spoken(
    publisher: Annotated[VoiceEventPublisher, Depends(get_voice_event_publisher)],
    session_id: Annotated[str, Query(alias="sessionId")],
    user_id: Annotated[int, Query(alias="userId")],
    body: UserSpokenRequest,
) -> dict[str, bool]:
    """手动触发一次"用户讲话"事件，验证异步监听者链路。

    接口会**立刻**返回 ``{"ok": true}``，监听者在后台任务里跑——日志中能看到
    ``[WARMUP]`` 和 ``[AUDIT]`` 两条输出。
    """
    publisher.publish_user_spoken(session_id, user_id, body.utterance, time.time())
    return {"ok": True}
