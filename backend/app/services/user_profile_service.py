"""用户画像服务：把静态表 + 动态表合成为 Agent 可用的快照。"""

from app.repositories.user_profile_repository import (
    UserProfileDynamicRepository,
    UserProfileStaticRepository,
)
from app.schemas.profile import UserProfileSnapshot
from app.services.cache_service import RedisCacheService


class UserProfileService:
    """读取并缓存用户画像快照。

    用 ``RedisCacheService`` 缓存快照——画像在一次会话期内基本不变，
    却被推荐 Agent 反复读取，每次都查库太浪费。
    """

    CACHE_PREFIX = "userProfileSnapshot:"

    def __init__(
        self,
        static_repo: UserProfileStaticRepository,
        dynamic_repo: UserProfileDynamicRepository,
        cache: RedisCacheService | None = None,
        cache_ttl_seconds: int = 1800,
    ) -> None:
        self._static_repo = static_repo
        self._dynamic_repo = dynamic_repo
        self._cache = cache
        self._cache_ttl_seconds = cache_ttl_seconds

    async def load(self, user_id: int) -> UserProfileSnapshot:
        """读取画像快照，命中缓存则直接返回。"""
        if self._cache is not None:
            cached = await self._cache.get(f"{self.CACHE_PREFIX}{user_id}")
            if cached is not None:
                return UserProfileSnapshot.model_validate(cached)

        snapshot = await self._load_from_db(user_id)

        if self._cache is not None:
            await self._cache.put(
                f"{self.CACHE_PREFIX}{user_id}",
                snapshot.model_dump(mode="json"),
                ttl_seconds=self._cache_ttl_seconds,
            )
        return snapshot

    async def _load_from_db(self, user_id: int) -> UserProfileSnapshot:
        static = await self._static_repo.find_by_id(user_id)
        dynamic = await self._dynamic_repo.find_by_id(user_id)

        return UserProfileSnapshot(
            user_id=user_id,
            gender=static.gender if static else None,
            age=static.age if static else None,
            height_cm=static.height_cm if static else None,
            weight_kg=static.weight_kg if static else None,
            skin_type=static.skin_type if static else None,
            budget_band=static.budget_band if static else None,
            category_affinity=dict(dynamic.category_affinity or {}) if dynamic else {},
            brand_affinity=dict(dynamic.brand_affinity or {}) if dynamic else {},
            recent_viewed=list(dynamic.recent_viewed or []) if dynamic else [],
            recent_purchased=list(dynamic.recent_purchased or []) if dynamic else [],
            price_sensitivity=dynamic.price_sensitivity if dynamic else None,
            avg_order_amount=dynamic.avg_order_amount if dynamic else None,
        )

    async def evict(self, user_id: int) -> None:
        """画像更新后主动失效缓存。"""
        if self._cache is not None:
            await self._cache.delete(f"{self.CACHE_PREFIX}{user_id}")
