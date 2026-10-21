"""用户画像契约（Agent 拿到的是合成好的快照，不关心底下怎么存）。"""

from decimal import Decimal

from pydantic import Field

from app.schemas.common import CamelModel


class UserProfileSnapshot(CamelModel):
    """静态画像 + 动态画像的合成快照。"""

    user_id: int
    gender: str | None = None
    age: int | None = None
    height_cm: int | None = None
    weight_kg: int | None = None
    skin_type: str | None = None
    budget_band: str | None = None
    category_affinity: dict[str, float] = Field(default_factory=dict)
    brand_affinity: dict[str, float] = Field(default_factory=dict)
    recent_viewed: list[int] = Field(default_factory=list)
    recent_purchased: list[int] = Field(default_factory=list)
    price_sensitivity: Decimal | None = None
    avg_order_amount: Decimal | None = None

    def to_natural_language(self) -> str:
        """生成一段适合塞进 Prompt 的自然语言。"""
        sb: list[str] = []
        if self.gender is not None:
            sb.append(f"性别{self.gender}，")
        if self.age is not None:
            sb.append(f"{self.age}岁，")
        if self.height_cm is not None and self.weight_kg is not None:
            sb.append(f"身高{self.height_cm}cm，体重{self.weight_kg}kg，")
        if self.budget_band is not None:
            sb.append(f"消费区间{self.budget_band}，")
        if self.category_affinity:
            top_cats = "，".join(
                f"{name}({weight:.2f})"
                for name, weight in sorted(
                    self.category_affinity.items(), key=lambda kv: kv[1], reverse=True
                )[:3]
            )
            sb.append(f"偏好品类：{top_cats}。")
        return "".join(sb)
