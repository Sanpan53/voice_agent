"""会话启动：由入口决定本次会话的商家范围。

这段逻辑放在 Service 而不是 Controller，理由是它包含
**三条业务规则**（哪种入口要必填什么参数、参数校验、落库 + 写缓存），
放 Controller 里会让那个文件同时承担 HTTP 契约和业务判断两件事。
Controller 现在只剩参数绑定。
"""

import logging

from app.repositories.product_repository import ProductRepository
from app.schemas.session import Channel, SessionScope
from app.services.session_scope_cache import SessionScopeCache
from app.services.session_service import SessionService

logger = logging.getLogger(__name__)


class SessionStartError(Exception):
    """启动会话的参数不合法。接口层翻成 400。"""


class SessionScopeService:
    """处理 ``POST /api/v1/session/start``。

    Args:
        products: 商品仓储——``PRODUCT_PAGE`` 入口要反查商品属于哪个商家。
        sessions: 会话主表服务。
        scope_cache: 范围缓存。
    """

    def __init__(
        self,
        products: ProductRepository,
        sessions: SessionService,
        scope_cache: SessionScopeCache,
    ) -> None:
        self._products = products
        self._sessions = sessions
        self._scope_cache = scope_cache

    async def start(
        self,
        session_id: str,
        user_id: int,
        channel: Channel,
        merchant_id: int | None = None,
        bound_product_id: int | None = None,
    ) -> SessionScope:
        """按入口类型算出商家范围，落库 + 写缓存。

        Raises:
            SessionStartError: 必填参数缺失，或 ``boundProductId`` 对应的商品不存在。
        """
        allowed = await self._resolve_allowed_merchants(channel, merchant_id, bound_product_id)

        # 顺带把 session 主表落地（用已有的幂等方法）
        await self._sessions.open_if_absent(
            session_id, user_id, channel.value, merchant_id, bound_product_id
        )

        scope = SessionScope(
            user_id=user_id,
            allowed_merchant_ids=allowed,
            bound_product_id=bound_product_id,
        )
        await self._scope_cache.put(session_id, scope)
        logger.info(
            "[Scope] session=%s channel=%s userId=%s allowed=%s",
            session_id,
            channel.value,
            user_id,
            allowed,
        )
        return scope

    async def _resolve_allowed_merchants(
        self,
        channel: Channel,
        merchant_id: int | None,
        bound_product_id: int | None,
    ) -> list[int] | None:
        """入口 → 允许的商家列表；``None`` 表示全平台。

        ``HOME_ENTRY`` 和 ``SEARCH_FALLBACK`` 走到最后自然落在 ``allowed = None``，
        不需要显式分支。
        """
        if channel is Channel.PRODUCT_PAGE:
            if bound_product_id is None:
                raise SessionStartError("PRODUCT_PAGE 必须带 boundProductId")
            product = await self._products.find_by_id(bound_product_id)
            if product is None:
                raise SessionStartError("商品不存在")
            return [product.merchant_id]

        if channel is Channel.MERCHANT_HOME:
            if merchant_id is None:
                raise SessionStartError("MERCHANT_HOME 必须带 merchantId")
            return [merchant_id]

        # HOME_ENTRY / SEARCH_FALLBACK → 全平台
        return None
