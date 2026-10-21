"""FAQ 仓储：按主键 / 已发布状态查询、写入向量、点击计数、最优命中检索。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.product import FaqEntry


class FaqRepository:
    """FAQ 表的数据访问。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_id(self, faq_id: int) -> FaqEntry | None:
        return await self._session.get(FaqEntry, faq_id)

    async def find_published(self, limit: int = 500) -> list[FaqEntry]:
        stmt = select(FaqEntry).where(FaqEntry.status == "PUBLISHED").limit(limit)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def update_embedding(self, faq_id: int, vector: list[float]) -> None:
        entry = await self._session.get(FaqEntry, faq_id)
        if entry is None:
            raise ValueError(f"FAQ 不存在: id={faq_id}")
        entry.embedding = vector
        await self._session.flush()

    async def increment_hit_count(self, faq_id: int) -> None:
        entry = await self._session.get(FaqEntry, faq_id)
        if entry is None:
            raise ValueError(f"FAQ 不存在: id={faq_id}")
        entry.hit_count += 1
        await self._session.flush()

    async def search_candidates(
        self, query_vector: list[float], merchant_id: int
    ) -> list[tuple[int, float]]:
        """向量最近邻：**每档各取最相似的一条**，商家自有档排在平台档前面。

        返回 ``[(faq_id, cosine_distance), ...]``，最多两行。

        这里刻意**不返回"全局唯一最优"**。之前是
        ``ORDER BY (merchant_id = :mid) DESC, distance LIMIT 1``，即先按归属排序、
        再比距离——商家只要有一条 FAQ，它就会**无条件**压掉平台 FAQ，
        哪怕平台那条是 1.0000 的精确命中。阈值又是在排序之后才判的，
        于是 ``merchantId=1`` 查"怎么开发票？"会得到 ``hit=false``：
        被一条 0.51 的商家 FAQ 顶掉了。

        正确做法是把"归属"降级成**分档**而不是**排序前缀**：两条候选都拿回来，
        由 :class:`~app.services.faq_vector_service.FaqVectorService` 先判阈值、
        再在过阈值的候选里按这个顺序取第一条。这样"商家优先"只在**都够像**时成立。

        实现用 PostgreSQL 的 ``DISTINCT ON (is_own)``：先按 ``is_own DESC`` 分档，
        档内按距离升序，每档保留第一条。注意 ``DISTINCT ON`` 的表达式必须与
        ``ORDER BY`` 最左侧一致，所以两边复用同一个 ``is_own`` 对象。
        """
        distance = FaqEntry.embedding.cosine_distance(query_vector)
        is_own = FaqEntry.merchant_id == merchant_id
        stmt = (
            select(FaqEntry.id, distance)
            .where(
                FaqEntry.status == "PUBLISHED",
                FaqEntry.embedding.is_not(None),
                FaqEntry.merchant_id.in_([0, merchant_id]),
            )
            .distinct(is_own)
            .order_by(is_own.desc(), distance)
        )
        result = await self._session.execute(stmt)
        return [(row.id, float(row[1])) for row in result.all()]
