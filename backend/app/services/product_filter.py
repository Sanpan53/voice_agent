"""把用户槽位翻译成结构化过滤条件。

过滤条件产出为**结构化过滤字典** :meth:`ProductFilter.to_query`，
交给 SQLAlchemy 仓储还原成带绑定参数的 ``WHERE``。

**安全性质是等价的**：条件值一律走绑定参数，不参与 SQL 文本拼接。Python 版反而更
进一步——调用方**根本没有**构造原生 SQL 的入口，所有受支持的过滤维度都必须在
``ProductRepository`` 里显式声明过。
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

#: 只有跑鞋需要额外的"鞋底缓震 + 地形"过滤
RUNNING_SHOE_CATEGORY = "跑鞋"


@dataclass(frozen=True)
class ScenarioRule:
    """场景 -> 商品属性条件。

    Attributes:
        cushion: 允许的缓震等级。
        terrain: 允许的地形。
        terrain_optional: ``terrain`` 是否允许缺失。数据里没标 terrain 是常态，
            所以公路场景用 ``IS NULL OR IN (...)`` 放行未标注商品，只挡掉明确
            冲突的类型（水泥路不能推 trail）；越野场景反过来要求必须标明 trail。
    """

    cushion: tuple[str, ...]
    terrain: tuple[str, ...]
    terrain_optional: bool


#: 场景映射表。``cushion`` 与 ``terrain`` 必须一起过滤——越野鞋的 cushion 常常也是
#: ``high``，单看 cushion 挡不住它，水泥路场景会被越野鞋误命中。
RUNNING_SHOE_SCENARIOS: dict[str, ScenarioRule] = {
    "水泥路": ScenarioRule(cushion=("high", "medium"), terrain=("road",), terrain_optional=True),
    "塑胶跑道": ScenarioRule(
        cushion=("medium", "low"), terrain=("road", "track"), terrain_optional=True
    ),
    "越野": ScenarioRule(cushion=("high",), terrain=("trail",), terrain_optional=False),
}


@dataclass(frozen=True)
class ProductFilter:
    """结构化过滤条件。"""

    category_l2: str | None = None
    max_price: float | None = None
    min_price: float | None = None
    brand: str | None = None
    genders: tuple[str, ...] = ()
    exclude_ids: tuple[int, ...] = ()
    #: JSONB 属性硬条件：``attributes->>key`` 必须命中给定取值之一
    attribute_in: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: JSONB 属性软条件：``attributes->>key`` 命中取值之一，**或属性缺失**
    attribute_optional_in: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        """是否不含任何过滤条件。"""
        return not (
            self.category_l2 is not None
            or self.max_price is not None
            or self.min_price is not None
            or self.brand is not None
            or self.genders
            or self.exclude_ids
            or self.attribute_in
            or self.attribute_optional_in
        )

    def to_query(self) -> dict[str, Any]:
        """转成 ``ProductRepository.hybrid_search`` 认的过滤字典。

        空条件不写进字典——仓储用 ``filters.get(...) is not None`` 判断是否加
        ``WHERE``，多写一个 ``None`` 只会让调用方误以为"有这个条件"。
        """
        query: dict[str, Any] = {}
        if self.category_l2 is not None:
            query["category_l2"] = self.category_l2
        if self.max_price is not None:
            query["max_price"] = self.max_price
        if self.min_price is not None:
            query["min_price"] = self.min_price
        if self.brand is not None:
            query["brand"] = self.brand
        if self.genders:
            query["genders"] = list(self.genders)
        if self.exclude_ids:
            query["exclude_ids"] = list(self.exclude_ids)
        if self.attribute_in:
            query["attribute_in"] = {
                name: list(values) for name, values in self.attribute_in.items()
            }
        if self.attribute_optional_in:
            query["attribute_optional_in"] = {
                name: list(values) for name, values in self.attribute_optional_in.items()
            }
        return query

    def merge(self, other: "ProductFilter") -> "ProductFilter":
        """按 AND 语义合并。

        结构化形态下同时设置同一个标量字段会得到互相矛盾的条件
        （``category_l2 = 'A' AND category_l2 = 'B'``），
        所以这里显式约定 **``other`` 优先**，映射字段按 key 覆盖。这里唯一的调用方是
        ``from_slots(...).merge(running_shoe_filter(...))``，两者字段集互不相交，
        这个约定只是把"万一撞了怎么办"写明白。
        """
        if self.is_empty:
            return other
        if other.is_empty:
            return self
        return ProductFilter(
            category_l2=other.category_l2 if other.category_l2 is not None else self.category_l2,
            max_price=other.max_price if other.max_price is not None else self.max_price,
            min_price=other.min_price if other.min_price is not None else self.min_price,
            brand=other.brand if other.brand is not None else self.brand,
            genders=other.genders or self.genders,
            exclude_ids=other.exclude_ids or self.exclude_ids,
            attribute_in={**self.attribute_in, **other.attribute_in},
            attribute_optional_in={**self.attribute_optional_in, **other.attribute_optional_in},
        )


def _as_number(value: Any) -> float | None:
    """非数值一律忽略。

    ``bool`` 在 Python 里是 ``int`` 的子类，所以显式排除——否则
    ``{"budget": True}`` 会被当成"预算 1 元"。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    return float(value)


def _as_int_tuple(value: Any) -> tuple[int, ...]:
    """把可迭代对象转成整数元组。

    字符串在 Python 里也是可迭代的，必须显式排除，否则
    ``excludeProductIds="123"`` 会被拆成 ``(1, 2, 3)``。
    """
    if value is None or isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        return ()
    out: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float, Decimal)):
            continue
        out.append(int(item))
    return tuple(out)


def from_slots(slots: dict[str, Any] | None) -> ProductFilter:
    """用户槽位 -> 过滤条件。"""
    if not slots:
        return ProductFilter()

    category = slots.get("category")
    brand = slots.get("brand")
    gender = slots.get("gender")

    return ProductFilter(
        category_l2=str(category) if category is not None else None,
        max_price=_as_number(slots.get("budget")),
        # 对比类场景（"再贵一点的"）的下界：把低于下界的都过滤掉
        min_price=_as_number(slots.get("priceMin")),
        brand=str(brand) if brand is not None else None,
        genders=(str(gender),) if gender is not None else (),
        # 排除上一轮已推过的商品，避免"换一款 / 便宜点"时又把同一批推回来
        exclude_ids=_as_int_tuple(slots.get("excludeProductIds")),
    )


def running_shoe_filter(slots: dict[str, Any] | None) -> ProductFilter:
    """跑鞋场景 -> 鞋底 / 地形条件。"""
    scenario = (slots or {}).get("scenario")
    # 场景槽位来自模型 JSON，类型不保证是字符串，
    # 显式 isinstance 防止不可哈希的取值（list / dict）在 dict.get 上抛 TypeError。
    if not isinstance(scenario, str):
        return ProductFilter()
    rule = RUNNING_SHOE_SCENARIOS.get(scenario)
    if rule is None:
        return ProductFilter()

    attribute_in: dict[str, tuple[str, ...]] = {"cushion": rule.cushion}
    attribute_optional_in: dict[str, tuple[str, ...]] = {}
    if rule.terrain_optional:
        attribute_optional_in["terrain"] = rule.terrain
    else:
        attribute_in["terrain"] = rule.terrain

    return ProductFilter(attribute_in=attribute_in, attribute_optional_in=attribute_optional_in)
