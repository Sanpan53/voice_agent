"""纯文本模拟接口。

语音链路太重——要麦克风、要 ASR、要听音频——调试 Orchestrator 时不该被这些拖住。
这个接口把同一套 ``OrchestratorService.handle`` 用文本方式跑一遍，返回完整的
``EmotionResult``（口播文案 + 商品卡片），是端到端验证的主要抓手。

```bash
curl -X POST "http://127.0.0.1:8080/api/v1/chat?sessionId=sess-1&userId=1" \\
     -H "Content-Type: text/plain" \\
     -d "最近想买点东西"
```

.. important::

   **``userId`` 查询参数的性质已经改了。** 原先它是无条件信任的——
   谁都能 ``?userId=2`` 冒充别人。现在它只在**认证未启用**（``AUTH_REQUIRED=false``，
   本地开发的默认值）时才被采纳；一旦带了合法 token，或认证开关打开，
   一律以 token 里的 userId 为准，查询参数直接忽略。判断逻辑收敛在
   :func:`app.security.current_user.resolve_request_user_id` 一处，
   与 WebSocket 入口共用。
"""

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, status

from app.api.dependencies import get_orchestrator_service
from app.schemas.emotion import EmotionResult
from app.security.current_user import UNAUTHORIZED_DETAIL, resolve_request_user_id
from app.security.tokens import extract_bearer_token
from app.services.orchestrator_service import OrchestratorService

router = APIRouter(prefix="/api/v1/chat", tags=["chat-debug"])


@router.post("", response_model=EmotionResult)
async def chat(
    orchestrator: Annotated[OrchestratorService, Depends(get_orchestrator_service)],
    session_id: Annotated[str, Query(alias="sessionId")],
    utterance: Annotated[str, Body(media_type="text/plain")],
    user_id: Annotated[int, Query(alias="userId")] = 0,
    authorization: Annotated[str | None, Header()] = None,
) -> EmotionResult:
    """跑一次完整的 Orchestrator 链路。

    Args:
        session_id: 会话 ID（多轮对话靠它串起来）。
        utterance: 用户这句话的文本。
        user_id: 调用方声明的用户 ID，默认 0。**认证开启或 token 有效时被忽略。**
        authorization: 可选的 ``Bearer`` 头。

    Raises:
        HTTPException: 401——认证开启但没带有效 token。
    """
    resolved = resolve_request_user_id(
        extract_bearer_token(authorization), fallback_user_id=user_id
    )
    if resolved is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=UNAUTHORIZED_DETAIL,
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await orchestrator.handle(session_id, resolved, utterance)
