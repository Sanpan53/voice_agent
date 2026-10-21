"""订单主表（``order_record`` 表）。

**当前实现只做读路径**——下单（写路径、状态机流转、库存扣减）尚未实现。
这里先把表结构立起来，是因为权限模型要有一个真实的受保护对象来验证。
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

#: 订单状态流转：CREATED → PAID → SHIPPED → COMPLETED，任意阶段可 CANCELLED。
STATUS_CREATED = "CREATED"
STATUS_PAID = "PAID"
STATUS_SHIPPED = "SHIPPED"
STATUS_COMPLETED = "COMPLETED"
STATUS_CANCELLED = "CANCELLED"


class OrderRecord(Base):
    """一笔订单。

    ``user_id`` 与 ``merchant_id`` 是**两个独立的归属维度**，这里的所有查询都必须
    至少绑定其中一个：

    + 用户查自己的订单 → 绑 ``user_id``；
    + 商家后台查自家订单 → 绑 ``merchant_id``。

    ``session_id`` 归因到具体会话，是"AI 导购到底带来了多少成交"这个指标的底座。
    """

    __tablename__ = "order_record"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_no: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    merchant_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    #: 归因会话，schema 里是 NOT NULL
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    product_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sku_code: Mapped[str] = mapped_column(String(64), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=STATUS_CREATED)
    #: AI 导购归因标记，schema 默认 TRUE
    agent_attribution: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    receiver_name: Mapped[str | None] = mapped_column(String(64))
    receiver_phone: Mapped[str | None] = mapped_column(String(32))
    receiver_addr: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
