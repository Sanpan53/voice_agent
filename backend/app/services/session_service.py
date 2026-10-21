"""会话主表服务：幂等开启会话。"""

from datetime import UTC, datetime

from app.models.session import Session
from app.repositories.session_repository import SessionRepository


class SessionService:
    """会话主表的幂等开启。

    首轮调用时落库，后续调用直接返回已有记录，保证写 ``session_state`` 时
    父表必然存在。
    """

    DEFAULT_CHANNEL = "HOME_ENTRY"
    DEFAULT_LOCALE = "zh_cn"

    def __init__(self, repo: SessionRepository) -> None:
        self._repo = repo

    async def open_if_absent(
        self,
        session_id: str,
        user_id: int,
        channel: str | None = None,
        merchant_id: int | None = None,
        bound_product_id: int | None = None,
    ) -> Session:
        """开启会话；已存在则原样返回（幂等）。

        Args:
            session_id: 会话 ID。
            user_id: 归属用户。
            channel: 入口类型，缺省 ``HOME_ENTRY``。
            merchant_id: 商家店铺入口对应的商家。
            bound_product_id: 商详页入口挂载的商品。

        Note:
            ``merchant_id`` / ``bound_product_id`` 是本表**已有**的两列，
            但早先的版本没有写它们——因为那时还没有"商家范围"这个概念，
            传进来的值无处可去。现在的 ``/session/start`` 是这两个字段
            唯一的写入时机，**再漏掉就永远是 NULL**，商品页进来说"这个怎么样"
            时也就无从指代。已存在的行**不覆盖**：``open_if_absent`` 是幂等的，
            重复调用不应改写首次进入时的上下文。
        """
        existing = await self._repo.find_by_id(session_id)
        if existing is not None:
            return existing

        entity = Session(
            id=session_id,
            user_id=user_id,
            merchant_id=merchant_id,
            channel=channel or self.DEFAULT_CHANNEL,
            bound_product_id=bound_product_id,
            started_at=datetime.now(UTC),
            locale=self.DEFAULT_LOCALE,
            total_tokens=0,
        )
        return await self._repo.save(entity)

    async def find_user_id(self, session_id: str) -> int | None:
        entity = await self._repo.find_by_id(session_id)
        return entity.user_id if entity is not None else None
