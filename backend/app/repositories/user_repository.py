"""用户仓储。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import STATUS_ACTIVE, AppUser


class AppUserRepository:
    """用户表的数据访问。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_username_and_status(
        self, username: str, status: str = STATUS_ACTIVE
    ) -> AppUser | None:
        """按用户名 + 状态查用户。

        状态参与查询条件（而不是查出来再判断）——已注销用户直接查不到。
        """
        stmt = select(AppUser).where(AppUser.username == username, AppUser.status == status)
        result = await self._session.execute(stmt)
        return result.scalars().first()

    async def find_by_id(self, user_id: int) -> AppUser | None:
        return await self._session.get(AppUser, user_id)
