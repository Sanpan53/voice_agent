"""Token 签发与校验。

采用 **JWT（PyJWT）**，也就是无状态方案：

+ 服务端**不存任何东西**，userId 直接编码在 token 里；
+ 水平扩容时不存在"这台机器上没有这个 token"的问题；
+ 代价是 **签发后无法单独撤销**。

> ⚠️ 选用无状态方案是一个有意的取舍：
> + 本项目只有"登录发 token、请求验 token"两个动作，没有踢人下线的需求；
> + 无状态换来的是不用为认证再加一处 Redis 依赖。
>
> 如果将来要做"改密码后强制所有设备重新登录"，需要补一张**吊销名单**
> （jti 黑名单，放 Redis 并设成 token 的剩余有效期），或者换回有状态方案。
"""

import logging
from datetime import UTC, datetime, timedelta

import jwt

from app.config import get_settings

logger = logging.getLogger(__name__)

#: JWT 里放 userId 的标准声明用 ``sub``（subject），值是字符串。
SUBJECT_CLAIM = "sub"


class TokenError(Exception):
    """签发 token 时配置不可用（不是"token 无效"，无效一律返回 ``None``）。"""


def tokens_available() -> bool:
    """当前配置能不能签发/校验 token。

    ``jwt_secret_key`` 默认为空（无生产默认值），此时认证能力**不可用**——
    由 :func:`app.security.current_user.get_current_user_id` 决定是放行还是拒绝。
    """
    return bool(get_settings().jwt_secret_key)


def issue_token(user_id: int) -> str:
    """签发一个携带 ``user_id`` 的 JWT。

    token 里的 ``sub`` 声明就是 userId。

    Raises:
        TokenError: ``jwt_secret_key`` 没配。这里**显式报错而不是用一个默认密钥**——
            默认密钥意味着任何人都能伪造 token，比不能用更危险。
    """
    settings = get_settings()
    if not settings.jwt_secret_key:
        raise TokenError("JWT_SECRET_KEY 未配置，无法签发 token")

    now = datetime.now(UTC)
    payload = {
        SUBJECT_CLAIM: str(user_id),
        "iat": now,
        "exp": now + timedelta(seconds=settings.token_expire_seconds),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str | None) -> int | None:
    """校验 token 并取出 userId；任何问题一律返回 ``None``。

    **"任何问题一律返回 None"是有意的**：过期、签名不对、格式错乱、userId 不是数字
    ……对调用方来说都是同一件事"这个 token 不能用"。区分它们只会给攻击者提供
    额外信息（"签名对了但过期了"说明密钥猜对了）。原始异常进日志，供排查用。

    注意不校验 ``sub`` 是不是真实存在的用户——那是 :class:`~app.services.auth_service.AuthService`
    或查询时的外键约束的事。token 里的 userId 查不到数据，自然就是空结果。
    """
    if not token or not get_settings().jwt_secret_key:
        return None

    settings = get_settings()
    try:
        payload = jwt.decode(
            token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm]
        )
    except jwt.PyJWTError as exc:
        logger.debug("token 校验失败: %s", exc)
        return None

    raw = payload.get(SUBJECT_CLAIM)
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning("token 的 sub 不是合法 userId: %r", raw)
        return None


def extract_bearer_token(header_value: str | None) -> str | None:
    """从 ``Authorization: Bearer xxx`` 头里取出 token。"""
    if not header_value:
        return None
    prefix = "Bearer "
    if not header_value.startswith(prefix):
        return None
    token = header_value[len(prefix) :].strip()
    return token or None


#: WebSocket 子协议里 token 的前缀。前端这么写：``new WebSocket(url, ['Bearer-' + token])``
SUBPROTOCOL_PREFIX = "Bearer-"


def extract_token_from_subprotocol(header_value: str | None) -> str | None:
    """从 ``Sec-WebSocket-Protocol`` 头里取出 token。

    **为什么 WebSocket 不用 ``Authorization`` 头**——浏览器的 ``WebSocket`` 构造
    函数**不允许自定义请求头**，所以 Cookie 之外的方案只剩"子协议"这一条路
    （RFC 6455 允许客户端在握手时列出想要的子协议）。

    不能对整个头做 ``startswith("Bearer-")``：客户端可能**只声明一个**子协议，
    但 RFC 允许声明多个（``Sec-WebSocket-Protocol: Bearer-xxx, chat``，某些代理
    也会这么改写）。所以这里先按逗号拆开、逐个匹配，只要求"其中一个是 Bearer-"，
    同时兼容单值的情况。
    """
    if not header_value:
        return None
    for raw in header_value.split(","):
        candidate = raw.strip()
        if candidate.startswith(SUBPROTOCOL_PREFIX):
            token = candidate[len(SUBPROTOCOL_PREFIX) :].strip()
            if token:
                return token
    return None
