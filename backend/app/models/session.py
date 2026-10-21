"""会话主表与状态机模型（session / session_state）。"""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Session(Base):
    """会话主表：一轮会话的元信息（谁、什么入口、什么时候开启）。

    ``session_state.session_id`` 是指向本表的外键，因此写 ``session_state`` 前
    必须先有父表记录。
    """

    __tablename__ = "session"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    merchant_id: Mapped[int | None] = mapped_column(BigInteger)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # HOME_ENTRY / PRODUCT_PAGE / SEARCH_FALLBACK
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    bound_product_id: Mapped[int | None] = mapped_column(BigInteger)
    locale: Mapped[str | None] = mapped_column(String(16))
    # ORDERED / ABANDONED / FOLLOWUP
    outcome: Mapped[str | None] = mapped_column(String(16))
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class SessionState(Base):
    """会话状态机：阶段 / 意图 / 槽位 / 待回答的澄清问题。"""

    __tablename__ = "session_state"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # INTENT / CLARIFY / RECOMMEND / ORDER_CONFIRM / ENDED
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    current_intent: Mapped[str | None] = mapped_column(String(64))
    # 槽位：品类、预算、风格、场景…… Agent 边聊边填
    slots: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # 当前还在等用户回答的那个澄清问题，没有则为 None
    pending_ask: Mapped[str | None] = mapped_column(Text)
    # 上一轮推给用户的商品 ID 列表，用于"再看看第二个"这类指代
    last_recommendations: Mapped[list[int] | None] = mapped_column(ARRAY(BigInteger))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
