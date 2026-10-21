"""异步数据库引擎与会话（SQLAlchemy 2 异步模式 + asyncpg）。"""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """惰性创建异步引擎（首次调用才构造，不立即连接数据库）。"""
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url, echo=False)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """惰性创建会话工厂。"""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：每个请求一个 ``AsyncSession``，**请求成功即提交**。

    .. important::

       **这个函数负责请求级的事务边界。** 早先它只
       ``yield session`` 就结束了——而 SQLAlchemy 的 ``AsyncSession`` 在
       ``async with`` 退出时调的是 ``close()``，**未提交的事务会被回滚**。
       仓储层全部只写 ``flush()``（把 SQL 发出去、拿到自增主键），没人
       ``commit()``，于是**此前所有 PG 写入都是写完就丢**：会话主表、
       会话状态机、用户画像、行为回流，一条都没真正落库。

       为什么一直没人发现：全部测试用的都是假仓储，没有一条测试碰过真实
       PostgreSQL；而链路里"读"的部分又能靠 Redis 和进程内缓存自圆其说。

       为什么到写订单路径才致命：前面写丢的是会话行、画像权重，丢了顶多
       "下次聊天不记得你"；**订单丢了是用户被明确告知"下单成功"之后钱货两空**。

    Python 没有框架层的事务管理，提交与回滚必须在这里自己处理——
    这就是这里要补上的关键一环。

    提交/回滚策略：

    + 正常返回 → ``commit()``：把这一轮的所有写入一次性落库；
    + 抛异常 → ``rollback()``：半个订单不能留在库里（比如扣了库存但没写订单）。

    Note:
        **WebSocket 路径的注意点**：FastAPI 对 WebSocket 端点同样会解析依赖，
        而 ``get_db`` 的 ``async with`` 会**持续整个连接的生命周期**——一条语音
        连接可能开好几分钟。也就是说语音链路此刻是"一个连接一个长事务"，
        这一点在 :meth:`app.services.order_service.OrderService.confirm` 里有
        针对性处理（下单**就地提交**，不等连接关闭）。详见项目文档的
        "尚未验证 / 已知问题"部分。
    """
    async with get_session_factory()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()
