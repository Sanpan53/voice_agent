"""订单仓储：**只暴露带主体的查询**。

这里的核心是一条约束，而不是几个方法：

> 业务 Service **禁止**调 ``orderRepo.findById(id)`` / ``findAll()``。
> 所有订单查询必须走带主体的方法。

做到这一点靠的是**不定义任何不带主体维度的方法**：少写一个方法，调用方就
无从绕过——比写一个 ``find_by_id`` 再靠 code review 提醒要可靠得多。

``tests/test_order_repository.py`` 里有一条测试专门断言"危险方法不存在"，
把这条约定从"注释里的规矩"升级成"改坏了会红"。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import OrderRecord


class OrderRepository:
    """订单表的数据访问。

    Note:
        **不要在这里加 ``find_by_id`` / ``find_all``**。哪怕只是想"调试时方便查一下"——
        加完它就会有人用，用了就是横向越权。需要按 id 查就带上主体维度。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- 列表查询：按主体 ---

    async def find_by_user_id_order_by_created_at_desc(
        self, user_id: int
    ) -> list[OrderRecord]:
        """某个用户的订单，按时间倒序。"""
        stmt = (
            select(OrderRecord)
            .where(OrderRecord.user_id == user_id)
            .order_by(OrderRecord.created_at.desc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def find_by_merchant_id_order_by_created_at_desc(
        self, merchant_id: int
    ) -> list[OrderRecord]:
        """某个商家的订单，按时间倒序。"""
        stmt = (
            select(OrderRecord)
            .where(OrderRecord.merchant_id == merchant_id)
            .order_by(OrderRecord.created_at.desc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    # --- 单个查询：id + 主体双绑定，避免横向越权 ---

    async def find_by_id_and_user_id(
        self, order_id: int, user_id: int
    ) -> OrderRecord | None:
        """查"我的某一笔订单"。

        两个条件必须同时满足。少了 ``user_id`` 就是横向越权——
        换个 id 就能看别人的订单。
        """
        stmt = select(OrderRecord).where(
            OrderRecord.id == order_id, OrderRecord.user_id == user_id
        )
        result = await self._session.execute(stmt)
        return result.scalars().first()

    async def find_by_id_and_merchant_id(
        self, order_id: int, merchant_id: int
    ) -> OrderRecord | None:
        """查"我家的某一笔订单"（商家后台视角）。"""
        stmt = select(OrderRecord).where(
            OrderRecord.id == order_id, OrderRecord.merchant_id == merchant_id
        )
        result = await self._session.execute(stmt)
        return result.scalars().first()

    # --- 写入 ---

    async def save(self, entity: OrderRecord) -> OrderRecord:
        """落一笔订单。

        **写入路径也有它的越权风险**——``user_id`` / ``merchant_id`` 必须来自
        服务端已经核实过的来源（待确认单 + 商品行），**绝不能来自请求参数**。
        改一笔订单的归属只需要改一个数字，而且改完是查不出来的
        （查询那边绑的正是这个被改掉的字段）。
        """
        self._session.add(entity)
        await self._session.flush()
        return entity

    async def commit(self) -> None:
        """**就地提交**当前事务。

        为什么不在 ``get_db`` 里统一提交就够了：WebSocket 端点的 ``get_db``
        依赖**活到连接关闭为止**（一条语音连接可能开好几分钟），而下单必须
        **立刻**落库——用户下一秒就会听到"下单成功"，然后可能就切走查订单去了。
        等连接关闭才提交，等于"告诉用户成功了但库里还没有"。
        """
        await self._session.commit()
