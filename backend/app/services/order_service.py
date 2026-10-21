"""订单服务：读路径 + 下单写路径。

**读路径**：查不到时返回 ``None``，由接口层翻成 **404 而不是 403**。

**写路径**：三段式 ``preview → confirm / cancel``——
先摊开预览单让用户确认，再真正落库扣库存。语音里"听到就下单"是不能接受的。

本服务在基础读写之外多做了三件事，都在方法 docstring 里：

1. ``preview`` 校验商品在**本次会话的商家范围**内（scope 一路管到下单）；
2. ``confirm`` 校验预览单**属于当前用户**（只按 sessionId 取的话，拿到别人的
   sessionId 就能确认别人的单）；
3. ``confirm`` 在**同一事务**内扣库存 + 写订单，并**就地提交**。
"""

import logging
import uuid

from app.models.order import STATUS_PAID, OrderRecord
from app.models.product import Product
from app.repositories.order_repository import OrderRepository
from app.repositories.product_repository import ProductRepository
from app.schemas.session import SessionScope
from app.services.long_term_memory_writer import LongTermMemoryWriter
from app.services.pending_order_store import PendingOrder, PendingOrderStore, build_pending_order
from app.services.user_behavior_sink import ProductPurchasedEvent, UserBehaviorSink

logger = logging.getLogger(__name__)


class OrderError(Exception):
    """下单流程中的业务错误。编排层把它翻成一句委婉的口播，**不把栈抛给用户**。"""


class OrderService:
    """订单读写。

    Args:
        repo: 订单仓储（只提供带主体维度的方法）。
        products: 商品仓储（查商品快照、扣库存）。
        pending: 预览单存储。
        memory_writer: 长期记忆回流器（下单成功 = 一次会话的有效结束）。
        behavior_sink: 行为回流（把购买写进动态画像）。
    """

    def __init__(
        self,
        repo: OrderRepository,
        products: ProductRepository | None = None,
        pending: PendingOrderStore | None = None,
        memory_writer: LongTermMemoryWriter | None = None,
        behavior_sink: UserBehaviorSink | None = None,
    ) -> None:
        self._repo = repo
        self._products = products
        self._pending = pending
        self._memory_writer = memory_writer
        self._behavior_sink = behavior_sink

    # ========== 读路径 ==========

    async def get_for_user(self, order_id: int, user_id: int) -> OrderRecord | None:
        """查"我的某一笔订单"。

        **查不到或不是自己的一律返回 ``None``**，由接口层转 404。这不是偷懒，
        是有意的：返回 403 等于告诉调用方"这个订单 id 在库里，只是不归你"——
        拿它当探针就能枚举出平台上有多少订单。404 的语义是"对调用方而言它不存在"，
        什么信息都不泄露。

        Returns:
            订单；不存在或不属于该用户时为 ``None``。
        """
        order = await self._repo.find_by_id_and_user_id(order_id, user_id)
        if order is None:
            # 日志里记的是"谁在什么时候探了哪个 id"——这是**审计**需要的，
            # 与响应体里返不返回原因无关。攻击者看不到日志，运维看得到。
            logger.info("订单不可见（不存在或非本人）orderId=%s userId=%s", order_id, user_id)
        return order

    async def list_mine(self, user_id: int) -> list[OrderRecord]:
        """我的所有订单（按时间倒序）。"""
        return await self._repo.find_by_user_id_order_by_created_at_desc(user_id)

    async def list_for_merchant(self, merchant_id: int) -> list[OrderRecord]:
        """商家后台查自家订单。

        .. warning::

           **这里先允许"传什么查什么"，真正的商家身份鉴权还没做（后续再补）。**
           要堵上这个洞需要：
           ① ``app_user`` 加商家绑定关系，或另建 ``merchant_user`` 关联表；
           ② 校验当前登录用户确实属于 ``merchantId`` 这个商家。
           在那之前**这个接口不能对外暴露**。
        """
        return await self._repo.find_by_merchant_id_order_by_created_at_desc(merchant_id)

    # ========== 写路径 ==========

    async def preview(
        self,
        session_id: str,
        user_id: int,
        product_id: int,
        quantity: int = 1,
        scope: SessionScope | None = None,
    ) -> PendingOrder:
        """生成预览单（**不扣库存、不写订单表**）。

        比基础实现多了一道 **scope 校验**：
        只按 ``product_id`` 直接查商品是不够的，没有商家范围检查就会越界下单。
        推荐列表本身是带 scope 的，所以正常路径下拿不到越界商品——但"正常路径下
        拿不到"不是一道防线。此前的结论是**每个能拿到商品的地方都要自己验**，
        下单尤其要：这是会写库、会扣库存的操作。

        Args:
            session_id: 会话 ID。
            user_id: 归属用户。
            product_id: 要下单的商品。
            quantity: 数量。
            scope: 本次会话的商家范围；``None`` 表示全平台（降级方向：
                商品可见性是"放宽"，**订单归属才是"拒绝"**）。

        Raises:
            OrderError: 商品不存在 / 不在本次会话的商家范围内 / 库存不足 /
                数量非法。
        """
        if quantity <= 0:
            raise OrderError("数量必须大于 0")

        product = await self._products.find_by_id(product_id) if self._products else None
        if product is None:
            raise OrderError("商品不存在")

        if not _in_scope(product, scope):
            # 越界与"不存在"对外**用同一句话术**——区别对待就等于告诉用户
            # "这个商品存在，只是你看不到"，那本身就是泄露。
            # 但日志级别要重：越界说明上游有问题，值得吵一声。
            logger.warning(
                "[Order] 越界下单尝试 sessionId=%s productId=%s merchantId=%s",
                session_id,
                product_id,
                product.merchant_id,
            )
            raise OrderError("商品不存在")

        if product.stock < quantity:
            raise OrderError("库存不足")

        pending = build_pending_order(
            session_id=session_id,
            user_id=user_id,
            product_id=product.id,
            merchant_id=product.merchant_id,
            product_name=product.name,
            sku_code=product.sku_code,
            quantity=quantity,
            unit_price=product.price,
        )

        if self._pending is not None:
            await self._pending.put(pending)

        logger.info(
            "[Order] 生成预览单 sessionId=%s productId=%s qty=%d total=%s",
            session_id,
            product.id,
            quantity,
            pending.total_amount,
        )
        return pending

    async def confirm(self, session_id: str, user_id: int) -> OrderRecord:
        """用户确认后**真正写单 + 扣库存**。

        三处关键设计：

        1. **归属校验**——不能只看会话 ID。会话 ID 是客户端生成、URL 里明文带的
           （见 ``voice_ws`` 的 ``sessionId`` query 参数），拿到别人的 sessionId
           就能确认别人的待下单。因此额外断言 ``pending.user_id == user_id``。
        2. **扣库存是条件 UPDATE**（见 ``ProductRepository.decrement_stock``），
           并发下不会超卖。
        3. **就地提交**——WebSocket 连接可能挂很久，不能等连接关闭才提交。

        Args:
            session_id: 会话 ID。
            user_id: 当前用户（来自凭证，**不是请求参数**）。

        Raises:
            OrderError: 没有待确认单 / 不属于当前用户 / 库存不足。
        """
        pending = await self._pending.get(session_id) if self._pending else None
        if pending is None:
            raise OrderError("没有待确认的订单，或已过期")
        if pending.user_id != user_id:
            # 对外话术与"没有待确认单"**完全一致**：不能告诉调用方
            # "这单存在，只是不是你的"，那又是一次存在性泄露（同为 404）。
            logger.warning(
                "[Order] 预览单归属不符 sessionId=%s owner=%s caller=%s",
                session_id,
                pending.user_id,
                user_id,
            )
            raise OrderError("没有待确认的订单，或已过期")

        # 先扣库存：条件 UPDATE 失败就说明这一件在被确认的这几秒里被别人买走了。
        # 此时**还没有写任何订单**，直接失败不会留下脏数据。
        decremented = (
            await self._products.decrement_stock(pending.product_id, pending.quantity)
            if self._products
            else False
        )
        if not decremented:
            logger.warning("[Order] 扣库存失败（并发抢购？）productId=%s", pending.product_id)
            raise OrderError("库存不足")

        order = OrderRecord(
            order_no=uuid.uuid4().hex,  # UUID 十六进制串，无横杠
            user_id=pending.user_id,
            merchant_id=pending.merchant_id,
            session_id=session_id,
            product_id=pending.product_id,
            sku_code=pending.sku_code,
            quantity=pending.quantity,
            unit_price=pending.unit_price,
            total_amount=pending.total_amount,
            status=STATUS_PAID,
            agent_attribution=True,
        )
        saved = await self._repo.save(order)

        # 提交时机：**写订单之后、发回流事件之前**。
        # 后面两个回流都会读写 PG（画像 / 记忆），订单还没提交的话，
        # 回流器可能读到一个"还不存在"的订单。
        await self._repo.commit()

        if self._pending is not None:
            await self._pending.remove(session_id)

        # 下单成功 = 一次会话的"有效结束"，触发两个回流
        await self._after_purchase(saved, pending)

        logger.info(
            "[Order] 下单成功 orderNo=%s userId=%s total=%s",
            saved.order_no,
            saved.user_id,
            saved.total_amount,
        )
        return saved

    async def peek_pending(self, session_id: str) -> PendingOrder | None:
        """看一眼有没有待确认的预览单（**不改状态、不删**）。

        编排层靠它区分"要新建一张"还是"在等用户确认"——这两种情况用户说的
        完全是同一个词（"好"），但该做的事完全不同。
        """
        if self._pending is None:
            return None
        return await self._pending.get(session_id)

    async def cancel(self, session_id: str) -> None:
        """用户反悔：删掉预览单。**不用回滚库存**——预览阶段本来就没扣。"""
        if self._pending is not None:
            await self._pending.remove(session_id)

    async def _after_purchase(self, order: OrderRecord, pending: PendingOrder) -> None:
        """下单后的两条回流，**任何一条失败都不能让订单失败**。

        订单已经落库并提交了，这时候再往外抛异常只会让用户听到"下单失败"，
        而实际上单已经下了——那才是真正难收拾的状态。
        """
        if self._behavior_sink is not None:
            try:
                # category 得反查商品：preview 和 confirm 是两次独立调用，
                # 预览单快照里没有存 category。多一次查询换"快照里少一个字段"，值得。
                product = (
                    await self._products.find_by_id(pending.product_id)
                    if self._products
                    else None
                )
                await self._behavior_sink.on_purchased(
                    ProductPurchasedEvent(
                        user_id=order.user_id,
                        product_id=order.product_id,
                        category=product.category_l2 if product is not None else None,
                        amount=float(order.total_amount),
                    )
                )
            except Exception:  # noqa: BLE001 — 画像回流失败不影响下单
                logger.error("购买行为回流失败 orderId=%s", order.id, exc_info=True)

        if self._memory_writer is not None:
            try:
                await self._memory_writer.flush_on_session_end(order.session_id, order.user_id)
            except Exception:  # noqa: BLE001 — 长期记忆回流失败不影响下单
                logger.error("下单后长期记忆回流失败 orderId=%s", order.id, exc_info=True)


def _in_scope(product: Product, scope: SessionScope | None) -> bool:
    """商品是否落在本次会话的商家范围内。

    与 :func:`app.services.scope_filter.build_scope_filter` 是**同一套判据**
    （``is_platform_wide``），但**不复用那个函数**：它产出的是给 SQL 用的过滤片段，
    这里是拿一行已经查出来的商品做判断。共用判据，不共用形态。
    """
    if scope is None or scope.is_platform_wide():
        return True
    return product.merchant_id in set(scope.allowed_merchant_ids or [])


__all__ = ["OrderError", "OrderService"]
