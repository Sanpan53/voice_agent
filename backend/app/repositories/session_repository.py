"""会话仓储：会话主表 + 会话状态机。"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.session import Session, SessionState


class SessionRepository:
    """会话主表的数据访问（主键为字符串）。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_id(self, session_id: str) -> Session | None:
        return await self._session.get(Session, session_id)

    async def save(self, entity: Session) -> Session:
        self._session.add(entity)
        await self._session.flush()
        return entity


class SessionStateRepository:
    """会话状态表的数据访问。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_id(self, session_id: str) -> SessionState | None:
        return await self._session.get(SessionState, session_id)

    async def save(self, state: SessionState) -> SessionState:
        # 已存在的行用 merge 覆盖，避免主键冲突。
        merged = await self._session.merge(state)
        await self._session.flush()
        return merged
