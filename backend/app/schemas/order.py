"""订单对外契约。"""

from datetime import datetime
from decimal import Decimal

from app.models.order import OrderRecord
from app.schemas.common import CamelModel


class OrderOut(CamelModel):
    """订单对外视图。

    **为什么不让接口直接返回 ORM 实体**：实体上有 ``agent_attribution``、
    ``session_id`` 这类内部归因字段，也有将来可能加的备注、风控标记。
    直接把实体序列化出去，等于**每次给表加列都自动对外暴露**——
    加个 ``internal_remark`` 就泄了。显式列出对外字段，
    新增列默认不对外，要暴露得有人主动加一行。
    """

    order_id: int
    order_no: str
    merchant_id: int
    product_id: int
    quantity: int
    unit_price: Decimal
    total_amount: Decimal
    status: str
    created_at: datetime

    @classmethod
    def from_record(cls, record: OrderRecord) -> "OrderOut":
        """实体 → 对外视图。"""
        return cls(
            order_id=record.id,
            order_no=record.order_no,
            merchant_id=record.merchant_id,
            product_id=record.product_id,
            quantity=record.quantity,
            unit_price=record.unit_price,
            total_amount=record.total_amount,
            status=record.status,
            created_at=record.created_at,
        )
