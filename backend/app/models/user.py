"""用户基础信息（``app_user`` 表）。

表名带 ``app_`` 前缀是为了避开 PostgreSQL 保留字 ``user``。
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

#: 有效状态。只有这个状态的用户能登录。
STATUS_ACTIVE = "ACTIVE"


class AppUser(Base):
    """登录主体。

    **演示项目里没有 password 字段**——登录只按 ``username`` + ``status`` 查。
    生产环境要换成密码哈希校验（bcrypt/argon2）或短信验证码，详见项目文档。
    """

    __tablename__ = "app_user"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    nickname: Mapped[str | None] = mapped_column(String(64))
    phone: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(128))
    #: ACTIVE / DISABLED（注销）
    status: Mapped[str] = mapped_column(String(16), default=STATUS_ACTIVE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
