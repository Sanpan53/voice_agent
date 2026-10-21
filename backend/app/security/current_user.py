"""当前登录用户。

没有 ThreadLocal 式的全局登录态，改成 **FastAPI 依赖**——
``Depends(get_current_user_id)`` 从请求头取 token 并解出 userId。
关键性质是：**业务代码从"当前请求的凭证"拿 userId，
而不是从方法参数拿**——后者就给了调用方伪造身份的机会。
"""

import logging
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from app.config import get_settings
from app.security.tokens import decode_token, extract_bearer_token, tokens_available

logger = logging.getLogger(__name__)

#: 认证失败统一用这个提示——不区分"没带 token"和"token 无效"，避免给探测者额外信息
UNAUTHORIZED_DETAIL = "未登录或登录已过期"


def get_optional_user_id(
    authorization: Annotated[str | None, Header()] = None,
) -> int | None:
    """取当前 userId；没登录返回 ``None``，不报错。

    给"登录可选"的接口用（如商品搜索——游客也能搜）。
    """
    return decode_token(extract_bearer_token(authorization))


def get_current_user_id(
    authorization: Annotated[str | None, Header()] = None,
) -> int:
    """取当前 userId；未登录或 token 无效一律 **401**。

    Raises:
        HTTPException: 401。与订单接口的 404 不同——这里泄露不了任何业务数据，
            401 是准确的语义。
    """
    user_id = decode_token(extract_bearer_token(authorization))
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=UNAUTHORIZED_DETAIL,
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user_id


#: 业务路由里更顺手的写法
CurrentUserId = Annotated[int, Depends(get_current_user_id)]
OptionalUserId = Annotated[int | None, Depends(get_optional_user_id)]


def _warn_if_open() -> None:
    """认证开关关着时，在启动日志里留一句显眼的提醒。

    允许"空密钥 = 不启用认证"，是为了让本地开发不用先配密钥就能把
    前面的链路跑起来。**上线前必须打开**，所以这里必须吵。
    """
    if not get_settings().auth_required:
        logger.warning(
            "认证未启用（AUTH_REQUIRED=false）：接口会直接信任调用方传入的 userId，"
            "仅限本地开发使用，生产环境务必开启"
        )


def resolve_request_user_id(
    token: str | None,
    fallback_user_id: int | None,
) -> int | None:
    """按认证开关决定用哪个 userId——**WebSocket 与文本调试接口共用的这一处**。

    走 token 时用它解出的 userId，直接调试时不带认证。
    把判断收在一个函数里，避免 ws 和 chat 两处各写一遍、
    哪天改了一处漏了另一处。

    Args:
        token: 从凭证里解出的 token（可能为 ``None``）。
        fallback_user_id: 调用方自己传的 userId（仅认证关闭时会被采纳）。

    Returns:
        最终生效的 userId；认证开启但 token 无效时返回 ``None``（调用方应拒绝）。
    """
    if get_settings().auth_required:
        # 认证开着：只认 token，调用方传什么都不看
        return decode_token(token)
    if tokens_available() and token:
        # 认证关着但带了 token：仍然按 token 走，方便本地同时验证两种模式
        decoded = decode_token(token)
        if decoded is not None:
            return decoded
    return fallback_user_id


__all__ = [
    "CurrentUserId",
    "OptionalUserId",
    "get_current_user_id",
    "get_optional_user_id",
    "resolve_request_user_id",
]
