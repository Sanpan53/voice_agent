"""登录接口。

```bash
curl -s -X POST http://localhost:8080/api/v1/auth/login \\
  -H "Content-Type: application/json" \\
  -d '{"username":"alice","password":"any"}'
# {"token":"eyJ...","userId":1}
```

.. note::

   **要跑通本接口必须先配 ``JWT_SECRET_KEY``**（``.env`` 里随便一串随机字符即可）。
   没配的话 :func:`app.security.tokens.issue_token` 会显式抛 ``TokenError``，
   接口返回 500——这是有意的，见该函数的说明：用默认密钥签发等于任何人都能伪造 token。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.dependencies import get_auth_service
from app.schemas.auth import LoginRequest, LoginResponse
from app.services.auth_service import AuthError, AuthService

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
async def login(
    request: LoginRequest,
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> LoginResponse:
    """按用户名签发 token。

    Raises:
        HTTPException: 401——用户不存在或已注销。
    """
    try:
        token, user_id = await service.login(request.username, request.password)
    except AuthError as exc:
        # 登录失败用 401；错误文案由 Service 统一给出，不区分"不存在"和"已注销"。
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    return LoginResponse(token=token, user_id=user_id)
