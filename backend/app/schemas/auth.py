"""认证契约。"""

from pydantic import Field

from app.schemas.common import CamelModel


class LoginRequest(CamelModel):
    """``POST /api/v1/auth/login`` 请求体。"""

    username: str = Field(min_length=1, max_length=64)
    #: **当前实现不校验密码**，见 :meth:`app.services.auth_service.AuthService.login`
    password: str | None = Field(default=None, max_length=128)


class LoginResponse(CamelModel):
    """登录响应。

    ``user_id`` 一并返回，前端就不必去解 JWT 的 payload——**让客户端解析 token
    是逼着它依赖 token 内部结构**，将来换签名算法或加字段都可能把前端弄崩。
    token 对客户端应当是不透明的字符串。
    """

    token: str
    user_id: int
