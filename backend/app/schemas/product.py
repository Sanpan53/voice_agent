"""商品相关契约（API 层使用）。"""

from typing import Any

from pydantic import Field

from app.schemas.common import CamelModel


class ProductOut(CamelModel):
    """商品视图（原 ProductEntity 的精简出口，不含向量列）。"""

    id: int = Field(gt=0)
    merchant_id: int | None = Field(default=None, description="商家隔离字段，平台商品可为空")
    name: str = Field(min_length=1, max_length=200)
    category_l1: str | None = Field(default=None, max_length=50)
    category_l2: str | None = Field(default=None, max_length=50)
    brand: str | None = Field(default=None, max_length=50)
    price: float = Field(ge=0)
    status: str = Field(default="ON_SALE", max_length=20)
    selling_points: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=2000)
    attributes: dict[str, Any] = Field(default_factory=dict)
