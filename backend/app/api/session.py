"""会话启动接口。

**这是"本次会话能看到哪些商家"的唯一入口**——范围一旦在这里定下来，
后面所有检索、推荐、下单都只能在这个范围内活动（强制过滤）。

```bash
TOKEN=$(curl -s -X POST http://localhost:8080/api/v1/auth/login \\
  -H "Content-Type: application/json" -d '{"username":"alice"}' | jq -r .token)

# 商详页进入 → allowedMerchantIds 锁定到该商品所属商家
curl -s -X POST http://localhost:8080/api/v1/session/start \\
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \\
  -d '{"sessionId":"demo-s1","channel":"PRODUCT_PAGE","boundProductId":6}'

# 首页进入 → allowedMerchantIds 为 null（全平台）
curl -s -X POST http://localhost:8080/api/v1/session/start \\
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \\
  -d '{"sessionId":"demo-s2","channel":"HOME_ENTRY"}'
```
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.dependencies import get_session_scope_service
from app.schemas.session import SessionScope, StartSessionRequest
from app.security.current_user import CurrentUserId
from app.services.session_scope_service import SessionScopeService, SessionStartError

router = APIRouter(prefix="/api/v1/session", tags=["session"])


@router.post("/start", response_model=SessionScope)
async def start_session(
    request: StartSessionRequest,
    user_id: CurrentUserId,
    service: Annotated[SessionScopeService, Depends(get_session_scope_service)],
) -> SessionScope:
    """按入口类型算出商家范围，落库并写缓存。

    Raises:
        HTTPException: 401 未登录；400 入口参数不合法（如 ``PRODUCT_PAGE``
            没带 ``boundProductId``，或该商品不存在）。
    """
    try:
        return await service.start(
            session_id=request.session_id,
            user_id=user_id,
            channel=request.channel,
            merchant_id=request.merchant_id,
            bound_product_id=request.bound_product_id,
        )
    except SessionStartError as exc:
        # 入口参数不合法统一翻成 400。
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
