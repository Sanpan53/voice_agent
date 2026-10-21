"""行为回流：把浏览 / 购买事件累加进动态画像。

这里采用**同步的事件处理函数**语义，由调用方决定放到后台任务还是消息队列里执行；
生产环境可以换成 Kafka 之类的消息队列。
"""

from dataclasses import dataclass

from app.models.user_profile import UserProfileDynamic
from app.repositories.user_profile_repository import UserProfileDynamicRepository

# 浏览加 0.05、购买加 0.15，经验值，跑久了根据真实转化数据再调。
VIEW_AFFINITY_DELTA = 0.05
PURCHASE_AFFINITY_DELTA = 0.15
MAX_RECENT_ITEMS = 100


@dataclass(frozen=True)
class ProductViewedEvent:
    user_id: int
    product_id: int
    category: str | None = None


@dataclass(frozen=True)
class ProductPurchasedEvent:
    user_id: int
    product_id: int
    category: str | None = None
    amount: float = 0.0


class UserBehaviorSink:
    """用户行为事件接收器。"""

    def __init__(self, repo: UserProfileDynamicRepository) -> None:
        self._repo = repo

    async def on_viewed(self, event: ProductViewedEvent) -> UserProfileDynamic:
        profile = await self._load_or_init(event.user_id)
        profile.recent_viewed = self._prepend(
            profile.recent_viewed, event.product_id
        )
        profile.category_affinity = self._bump_affinity(
            profile.category_affinity, event.category, VIEW_AFFINITY_DELTA
        )
        return await self._repo.save(profile)

    async def on_purchased(self, event: ProductPurchasedEvent) -> UserProfileDynamic:
        profile = await self._load_or_init(event.user_id)
        profile.recent_purchased = self._prepend(
            profile.recent_purchased, event.product_id
        )
        profile.category_affinity = self._bump_affinity(
            profile.category_affinity, event.category, PURCHASE_AFFINITY_DELTA
        )
        return await self._repo.save(profile)

    async def _load_or_init(self, user_id: int) -> UserProfileDynamic:
        existing = await self._repo.find_by_id(user_id)
        if existing is not None:
            return existing
        return UserProfileDynamic(
            user_id=user_id,
            category_affinity={},
            brand_affinity={},
            recent_viewed=[],
            recent_purchased=[],
        )

    @staticmethod
    def _prepend(existing: list[int] | None, product_id: int) -> list[int]:
        """新商品插到列表头部，最多保留 100 条。"""
        items = [product_id, *(existing or [])]
        return items[:MAX_RECENT_ITEMS]

    @staticmethod
    def _bump_affinity(
        affinity: dict[str, float] | None, category: str | None, delta: float
    ) -> dict[str, float]:
        """品类权重累加，上限 1.0。"""
        current = dict(affinity or {})
        if category is None:
            return current
        current[category] = min(1.0, current.get(category, 0.0) + delta)
        return current
