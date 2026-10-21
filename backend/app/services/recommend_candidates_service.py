"""候选召回：向量检索 + 属性过滤，产出打包好的 :class:`RecommendedItem` 列表。

:class:`~app.services.product_search_service.ProductSearchService` 早就
直接返回商品行（pgvector 就在 PG 里，过滤条件和向量检索是同一条 SQL），没有
"先取 ID 再回查"的两次查询；但**兜底过滤的意图保留下来了**——即使上游漏了
过滤条件，也要在结果上再挡一刀，见 :meth:`_drop_out_of_scope`。
"""

import logging
from typing import Any

from app.schemas.recommend import RecommendedItem
from app.schemas.session import SessionScope
from app.services.product_filter import (
    RUNNING_SHOE_CATEGORY,
    from_slots,
    running_shoe_filter,
)
from app.services.product_search_service import ProductSearchService
from app.services.scope_filter import build_scope_filter

logger = logging.getLogger(__name__)

#: 初筛条数：先多召一些，后面 Rerank 再收窄到 3-5
CANDIDATE_TOP_N = 20

#: 初始分按向量检索顺序递减的步长
INITIAL_SCORE_STEP = 0.03


class RecommendCandidatesService:
    """检索出候选集，打包好基础信息给下游。"""

    def __init__(self, search: ProductSearchService) -> None:
        self._search = search

    async def fetch_candidates(
        self,
        query: str,
        slots: dict[str, Any] | None,
        top_n: int = CANDIDATE_TOP_N,
        scope: SessionScope | None = None,
    ) -> list[RecommendedItem]:
        """按查询文本召回候选商品。

        Args:
            query: 检索文本（用户原话 + 场景词）。
            slots: 意图 Agent 抽出的槽位。
            top_n: 召回条数。
            scope: 本次会话的商家范围。``None`` 表示全平台——这是**为了兼容老调用方**
                选的默认值（不传就是原来的行为），推荐链路一定会显式传。
        """
        product_filter = from_slots(slots)
        if (slots or {}).get("category") == RUNNING_SHOE_CATEGORY:
            product_filter = product_filter.merge(running_shoe_filter(slots))

        # 叠加商家范围。全平台时 :func:`build_scope_filter` 返回空字典，不影响原条件。
        query_filters = product_filter.to_query()
        query_filters.update(build_scope_filter(scope))

        products = await self._search.search(
            query,
            top_k=top_n,
            filters=query_filters or None,
        )

        visible = self._drop_out_of_scope(products, scope)

        return [
            RecommendedItem(
                product_id=product.id,
                name=product.name,
                price=product.price,
                reason=None,  # 理由生成阶段回填
                match_score=1.0 - index * INITIAL_SCORE_STEP,
                attributes=product.attributes or {},
            )
            for index, product in enumerate(visible)
        ]

    @staticmethod
    def _drop_out_of_scope(products: list, scope: SessionScope | None) -> list:
        """兜底过滤：丢掉不属于允许商家的商品。

        这就是兜底过滤（"即使上游漏了，这一层也挡住"），在结果上再挡一刀。

        **两道防线都要有**：SQL 那层负责性能（在库里就少捞数据），这层负责正确性
        （不依赖任何 ``ProductSearchService`` 实现都老实透传 ``filters``——
        它是个 ABC，将来接 Milvus 时很容易漏）。

        命中时记 ``warning`` 而不是 ``debug``：这层**正常情况下一件都不该拦到**，
        拦到就说明上游出问题了，值得吵一声。
        """
        if scope is None or scope.is_platform_wide():
            return list(products)

        allowed = set(scope.allowed_merchant_ids or [])
        kept = [p for p in products if p.merchant_id in allowed]
        dropped = len(products) - len(kept)
        if dropped:
            logger.warning("[Scope] 兜底过滤掉 %d 个越界商品（上游检索未生效？）", dropped)
        return kept
