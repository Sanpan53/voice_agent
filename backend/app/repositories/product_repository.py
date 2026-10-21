"""商品仓储：按主键 / 状态查询、写入向量、混合向量检索。"""

from typing import Any

from sqlalchemy import Select, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.product import Product

# 混合检索允许的过滤键。
# 白名单是硬约束：过滤键只允许来自这里，其余一律抛 ValueError——调用方压根
# 构造不出 SQL，比拼接 SQL 片段更安全。
ALLOWED_FILTERS = {
    # 已有
    "category_l2",
    "max_price",
    "genders",
    # 新增：对比场景下界 / 品牌 / 排除项 / JSONB 属性条件
    "min_price",
    "brand",
    "exclude_ids",
    "attribute_in",
    "attribute_optional_in",
    # 新增：多商家数据隔离。由 ScopeFilterBuilder 产出，
    # 但这里**不假设**调用方一定是它——白名单只管"这个维度受支持"。
    "merchant_ids",
}


class ProductRepository:
    """商品表的数据访问。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_ids(self, ids: list[int]) -> list[Product]:
        """按主键批量查询，并按传入顺序返回（IN 查询本身不保证顺序）。"""
        if not ids:
            return []
        stmt = select(Product).where(Product.id.in_(ids))
        result = await self._session.execute(stmt)
        rows = list(result.scalars().all())
        by_id = {row.id: row for row in rows}
        return [by_id[i] for i in ids if i in by_id]

    async def find_by_status(self, status: str, limit: int = 500) -> list[Product]:
        stmt = select(Product).where(Product.status == status).limit(limit)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def find_by_id(self, product_id: int) -> Product | None:
        """按主键查单个商品。"""
        return await self._session.get(Product, product_id)

    async def decrement_stock(self, product_id: int, quantity: int) -> bool:
        """扣减库存；库存不足时**一件都不扣**，返回 ``False``。

        实现是一条**带条件的 UPDATE**：``WHERE stock >= :qty`` 写在 SQL 里，
        而不是"先 SELECT 出来判断再 UPDATE"。两者在并发下完全不同：

        + 先读后写 → 两个请求都读到 ``stock=1``，都认为够，都写 ``stock=0``
          ——**超卖一件**，而且两边都以为成功了；
        + 条件 UPDATE → 数据库的行锁保证只有一个 ``UPDATE`` 能匹配到，
          另一个的 ``rowcount`` 是 0，如实返回"库存不足"。

        这里刻意不采用"先读出来、改内存、再整行写回"的做法——那是典型的
        读改写，并发下会超卖。

        Note:
            这里**只 flush 不 commit**，提交时机由调用方（``OrderService.confirm``）
            决定——库存扣减和订单写入必须在同一个事务里，否则会出现
            "扣了库存但订单没落库"。

        Returns:
            ``True`` 扣减成功；``False`` 库存不足或商品不存在。
        """
        stmt = (
            update(Product)
            .where(Product.id == product_id, Product.stock >= quantity)
            .values(stock=Product.stock - quantity)
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return bool(result.rowcount)

    async def update_embedding(self, product_id: int, vector: list[float]) -> None:
        """只更新向量列（普通商品流程不加载 embedding，避免读 4KB 向量）。"""
        product = await self._session.get(Product, product_id)
        if product is None:
            raise ValueError(f"商品不存在: id={product_id}")
        product.embedding = vector
        await self._session.flush()

    async def hybrid_search(
        self,
        query_vector: list[float],
        filters: dict[str, Any] | None = None,
        top_k: int = 10,
    ) -> list[Product]:
        """向量相似度 + 结构化过滤的混合检索（cosine 距离，距离越小越相似）。"""
        if not 1 <= top_k <= 100:
            raise ValueError("top_k 必须在 1-100 之间")

        distance = Product.embedding.cosine_distance(query_vector)
        stmt: Select[tuple[Product]] = select(Product).where(
            Product.status == "ON_SALE", Product.embedding.is_not(None)
        )

        filters = filters or {}
        for key in filters:
            if key not in ALLOWED_FILTERS:
                raise ValueError(f"不支持的过滤条件: {key}")

        category_l2 = filters.get("category_l2")
        if category_l2:
            stmt = stmt.where(Product.category_l2 == category_l2)

        max_price = filters.get("max_price")
        if max_price is not None:
            stmt = stmt.where(Product.price <= max_price)

        min_price = filters.get("min_price")
        if min_price is not None:
            stmt = stmt.where(Product.price >= min_price)

        brand = filters.get("brand")
        if brand:
            stmt = stmt.where(Product.brand == brand)

        genders = filters.get("genders")
        if genders:
            # JSONB 字段：attributes->>'gender' 命中所选人群。
            stmt = stmt.where(Product.attributes["gender"].as_string().in_(genders))

        exclude_ids = filters.get("exclude_ids")
        if exclude_ids:
            stmt = stmt.where(Product.id.not_in(exclude_ids))

        # JSONB 属性硬条件：attributes->>key 必须命中（如跑鞋的缓震等级）
        for name, values in (filters.get("attribute_in") or {}).items():
            stmt = stmt.where(Product.attributes[name].as_string().in_(values))

        # JSONB 属性软条件：命中 或 属性缺失。数据没标 terrain 是常态，放行未标注的
        # 商品，只挡掉与场景明确冲突的取值（水泥路不推 trail）。
        for name, values in (filters.get("attribute_optional_in") or {}).items():
            column = Product.attributes[name].as_string()
            stmt = stmt.where(or_(column.is_(None), column.in_(values)))

        merchant_ids = filters.get("merchant_ids")
        if merchant_ids:
            # 数据隔离的硬过滤。**空列表在这里等于不加条件**——
            # 与 ``SessionScope.is_platform_wide()`` 的语义一致，见该方法的说明。
            # 这个"空列表不过滤"的行为由测试单独钉住：它是整个隔离机制里
            # 唯一一处"写错了也不会报错、只会静默放行"的地方。
            stmt = stmt.where(Product.merchant_id.in_(merchant_ids))

        stmt = stmt.order_by(distance).limit(top_k)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
