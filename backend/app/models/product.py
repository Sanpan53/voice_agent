"""商品模型（原 product / faq_entry 表，含 pgvector 向量列）。"""

from datetime import datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, DateTime, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

# 向量维度：与配置 Settings.embedding_dim（text-embedding-v3 默认 1024）保持一致。
EMBEDDING_DIM = 1024


class Product(Base, TimestampMixin):
    __tablename__ = "product"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 商家隔离字段：这里只用普通列，多商家外键约束留到后续再补。
    merchant_id: Mapped[int] = mapped_column(nullable=False, index=True)
    sku_code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    category_l1: Mapped[str] = mapped_column(String(32), nullable=False)
    category_l2: Mapped[str] = mapped_column(String(64), nullable=False)
    brand: Mapped[str | None] = mapped_column(String(64))
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    original_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    stock: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ON_SALE", nullable=False)
    selling_points: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    # 品类属性各异，统一 JSONB + GIN 索引，支持 attributes->>'gender' 这类过滤
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    is_new_arrival: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # deferred=True：普通商品查询不读取约 4KB 的 1024 维向量。
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIM), nullable=True, deferred=True
    )


class FaqEntry(Base):
    """FAQ 知识库（原 faq_entry 表，承接非导购高频问题）。

    merchant_id = 0 表示平台通用 FAQ，检索时写 ``merchant_id IN (0, :merchant)``。

    **没有 ``created_at``**——``doc/schema.sql`` 里这张表只有 ``updated_at``，
    FAQ 是"内容表"不是"事件表"，什么时候写的没有意义，只有"什么时候改过"有用。
    这里刻意不复用 :class:`TimestampMixin`：那个 mixin 会带上 ``created_at``，
    于是按 ``schema.sql`` 建出来的库一查这张表就
    ``UndefinedColumnError: column faq_entry.created_at does not exist``。
    ``user_profile_static`` / ``user_profile_dynamic`` / ``session_state``
    在 schema 里也是同样的"只有 updated_at"约定。
    """

    __tablename__ = "faq_entry"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    merchant_id: Mapped[int] = mapped_column(default=0, nullable=False, index=True)
    question: Mapped[str] = mapped_column(String(512), nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="PUBLISHED", nullable=False)
    hit_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIM), nullable=True, deferred=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
