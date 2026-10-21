"""用户画像仓储：静态表与动态表各一个，按主键查询。"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user_profile import UserProfileDynamic, UserProfileStatic


class UserProfileStaticRepository:
    """静态画像表的数据访问。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_id(self, user_id: int) -> UserProfileStatic | None:
        return await self._session.get(UserProfileStatic, user_id)

    async def save(self, profile: UserProfileStatic) -> UserProfileStatic:
        self._session.add(profile)
        await self._session.flush()
        return profile


class UserProfileDynamicRepository:
    """动态画像表的数据访问。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_id(self, user_id: int) -> UserProfileDynamic | None:
        return await self._session.get(UserProfileDynamic, user_id)

    async def save(self, profile: UserProfileDynamic) -> UserProfileDynamic:
        self._session.add(profile)
        await self._session.flush()
        return profile
