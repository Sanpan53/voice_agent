"""待确认订单（预览单）的存储。

**预览态是语音下单的关键一环**：Agent 不能听到"要第二款"就直接扣库存写单——
必须先摊开"商品 + 单价 + 合计"，等用户明确说"确认"再落库。这个中间态就是
``PendingOrder``。

放 Redis 而不是 PG，理由和 ``SessionScope`` 一样：它是**会话期间的旁路数据**，
短 TTL（默认 10 分钟）后自然消失，不需要进业务表、也不该留下"半张订单"的脏数据。

> 📌 与 ``voice_agent:scope:`` 的对比：
> + ``voice_agent:scope:{id}`` —— 一次写入、全程不变，TTL 跟会话走（30 分钟）；
> + ``voice_agent:pending_order:{id}`` —— 会随"换一款"改写，TTL 更短（10 分钟），
>   且**用户确认或取消后要主动删掉**。
"""

import logging
from decimal import Decimal

from pydantic import ValidationError
from redis.asyncio import Redis

from app.schemas.common import CamelModel

logger = logging.getLogger(__name__)

#: Redis key 前缀，后接 ``sessionId``。
PENDING_ORDER_KEY_PREFIX = "voice_agent:pending_order:"

#: 预览单的存活时间（分钟）。**比会话 TTL 短**——用户说"我再想想"之后
#: 十分钟还挂着一张锁着库存的单子，不如让它自己过期。
PENDING_ORDER_TTL_MINUTES = 10


class PendingOrder(CamelModel):
    """一张"还没确认"的订单。

    **商品名和单价都冗余存了一份**
    ——不是偷懒：确认页要用它们渲染话术，而商品信息在用户确认之前可能被商家改价。
    存快照才能保证"用户看到的价格"就是"下单的价格"，不会出现"确认时悄悄涨了 20 块"。

    Attributes:
        session_id: 归属会话。
        user_id: 归属用户（**确认时必须校验**，见 ``OrderService.confirm``）。
        merchant_id: 商品所属商家。
        product_id: 商品 ID。
        product_name: 商品名快照。
        sku_code: SKU 快照。
        quantity: 数量。
        unit_price: 单价快照。
        total_amount: 合计 = 单价 × 数量。
    """

    session_id: str
    user_id: int
    merchant_id: int
    product_id: int
    product_name: str
    sku_code: str
    quantity: int
    unit_price: Decimal
    total_amount: Decimal


class PendingOrderStore:
    """``PendingOrder`` 的读写。

    Args:
        redis: Redis 客户端。
        ttl_minutes: 过期时间，默认 10 分钟。
    """

    def __init__(self, redis: Redis, ttl_minutes: int | None = None) -> None:
        self._redis = redis
        self._ttl_minutes = ttl_minutes or PENDING_ORDER_TTL_MINUTES

    def _key(self, session_id: str) -> str:
        return f"{PENDING_ORDER_KEY_PREFIX}{session_id}"

    async def put(self, order: PendingOrder) -> None:
        """写入预览单，并重置 TTL。"""
        await self._redis.set(
            self._key(order.session_id),
            order.model_dump_json(by_alias=True),
            ex=self._ttl_minutes * 60,
        )

    async def get(self, session_id: str) -> PendingOrder | None:
        """读取预览单；不存在或解析失败都返回 ``None``。

        **解析失败必须返回 ``None`` 而不是抛异常**：Redis 里躺着一条格式不对的
        数据（改过字段名、被手工写脏）时，用户说"确认"应当得到"没有待确认的订单"，
        而不是一个 500——这里捕获解析异常并返回 ``None``。
        """
        raw = await self._redis.get(self._key(session_id))
        if raw is None:
            return None
        try:
            return PendingOrder.model_validate_json(raw)
        except ValidationError as exc:
            logger.warning("预览单解析失败 sessionId=%s: %s", session_id, exc)
            return None

    async def remove(self, session_id: str) -> None:
        """删除预览单。确认成功、用户取消、改主意换款时都要调。"""
        await self._redis.delete(self._key(session_id))


def build_pending_order(
    session_id: str,
    user_id: int,
    product_id: int,
    merchant_id: int,
    product_name: str,
    sku_code: str,
    quantity: int,
    unit_price: Decimal,
) -> PendingOrder:
    """按商品快照 + 数量拼一张预览单，**合计在这里算一次**。

    单独抽出来是因为"合计怎么算"必须只有一处实现——将来要做优惠券 / 满减，
    改漏一处就会出现"预览 479、下单 429"的对不上。
    """
    return PendingOrder(
        session_id=session_id,
        user_id=user_id,
        merchant_id=merchant_id,
        product_id=product_id,
        product_name=product_name,
        sku_code=sku_code,
        quantity=quantity,
        unit_price=unit_price,
        total_amount=unit_price * quantity,
    )
