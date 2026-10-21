"""把 ``SessionScope`` 翻译成检索过滤条件。

产出 ``{"merchant_ids": [3, 7]}`` 这样一个结构化字典——具体怎么变成 ``WHERE``
由 :class:`~app.repositories.product_repository.ProductRepository` 的
**白名单**决定。安全性质等价（值一律走绑定参数），而且调用方压根构造不出 SQL。

**为什么要在 Service 层集中加**：不依赖每个 Agent 自己写对。检索入口可能有好几个
（推荐、对比、搜索兜底），只要有一处忘了带 scope，隔离就漏了。收在一个函数里，
所有入口都得调它。
"""

import logging
from typing import TYPE_CHECKING, Any

from app.schemas.session import SessionScope

if TYPE_CHECKING:  # 仅为类型标注，避免运行期循环 import
    from app.services.session_scope_cache import SessionScopeCache

logger = logging.getLogger(__name__)


def build_scope_filter(scope: SessionScope | None) -> dict[str, Any]:
    """生成商家隔离的过滤片段。

    Returns:
        全平台时返回 ``{}``（空片段，叠加后不影响原有条件）；否则返回
        ``{"merchant_ids": [...]}``。
    """
    if scope is None or scope.is_platform_wide():
        return {}
    return {"merchant_ids": list(scope.allowed_merchant_ids or [])}


def allowed_merchant_ids(scope: SessionScope | None) -> list[int] | None:
    """取"允许的商家 ID 列表"，全平台时为 ``None``。

    给需要**在 PG 层再兜一刀**的地方用（详情回查）：全平台返回 ``None``，
    否则返回允许的商家列表。
    """
    if scope is None or scope.is_platform_wide():
        return None
    return list(scope.allowed_merchant_ids or [])


async def resolve_scope(
    cache: "SessionScopeCache | None",
    session_id: str,
    user_id: int,
) -> SessionScope:
    """取会话范围，取不到就降级为全平台。

    **没取到时的降级方向是"全平台"**。这个方向值得说明：

    + 降级成"全平台" = **放宽**权限。看起来危险，但这里是对的——scope 是"收窄"
      用的，它由入口决定；没有 scope 说明这次会话压根没走过 ``/session/start``
      （老会话、直连调试、Redis key 已过期），此时并没有任何"用户只想看某商家"
      的意图，平台的正常语义就是全平台。
    + 反过来降级成"什么都看不到"会让所有老会话和调试入口直接瘫掉，而且
      **用户看到的是空结果、不是报错**，排查起来极其费劲。

    真正的权限边界在**订单**那边——那里的降级方向是"拒绝"，因为订单没有
    "平台通用"这个语义。**两者的降级方向不同是有意的**，不是不一致。

    Note:
        顺序版和并行版推荐链路**共用这一个函数**——这种"两边都要写对、漏一处就漏权限"
        的逻辑，重复一次就是多一个出事的地方。
    """
    if cache is None:
        return SessionScope(user_id=user_id)
    scope = await cache.get(session_id)
    if scope is not None:
        return scope
    logger.debug("会话无 scope，降级为全平台 sessionId=%s", session_id)
    return SessionScope(user_id=user_id)
