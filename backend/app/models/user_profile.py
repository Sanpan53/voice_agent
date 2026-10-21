"""用户画像双表模型：静态画像 + 动态画像。"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class UserProfileStatic(Base):
    """静态画像：用户自己填的，几个月才变一次。

    主键即 ``user_id``。
    """

    __tablename__ = "user_profile_static"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    gender: Mapped[str | None] = mapped_column(String(255))
    age: Mapped[int | None] = mapped_column(Integer)
    city: Mapped[str | None] = mapped_column(String(255))
    height_cm: Mapped[int | None] = mapped_column(Integer)
    weight_kg: Mapped[int | None] = mapped_column(Integer)
    skin_type: Mapped[str | None] = mapped_column(String(255))
    tech_savvy: Mapped[str | None] = mapped_column(String(255))
    budget_band: Mapped[str | None] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class UserProfileDynamic(Base):
    """动态画像：行为累加出来的，每天更新。

    ``category_affinity`` / ``brand_affinity`` 是 JSONB 权重表；
    ``recent_viewed`` / ``recent_purchased`` 对应 PG 的 ``bigint[]``。
    """

    __tablename__ = "user_profile_dynamic"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    category_affinity: Mapped[dict[str, float] | None] = mapped_column(JSONB)
    brand_affinity: Mapped[dict[str, float] | None] = mapped_column(JSONB)
    recent_viewed: Mapped[list[int] | None] = mapped_column(ARRAY(BigInteger))
    recent_purchased: Mapped[list[int] | None] = mapped_column(ARRAY(BigInteger))
    price_sensitivity: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    avg_order_amount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
