"""记忆回流调试接口。

三个自动触发点（WebSocket 关闭、订单确认、Redis TTL 过期）开发阶段都不方便
反复验证——开发机上 WebSocket 一直连着、TTL 默认 30 分钟、订单链路还没打通。
所以补一个手动入口，把回流链路单独跑通看看。

.. danger::

   **生产环境这个接口要挂在内网管理域名下、加鉴权，别暴露到公网。**
   它能凭一个 sessionId 直接改写用户画像。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.agent.agent_memory_guard import AgentMemoryGuard
from app.api.dependencies import (
    get_agent_memory_guard,
    get_long_term_memory_writer,
    get_session_service,
)
from app.services.long_term_memory_writer import LongTermMemoryWriter
from app.services.session_service import SessionService

router = APIRouter(prefix="/api/v1/debug/memory", tags=["memory-debug"])


@router.post("/flush")
async def flush(
    writer: Annotated[LongTermMemoryWriter, Depends(get_long_term_memory_writer)],
    session_service: Annotated[SessionService, Depends(get_session_service)],
    session_id: Annotated[str, Query(alias="sessionId")],
    user_id: Annotated[int | None, Query(alias="userId")] = None,
) -> dict:
    """手动触发一次长期记忆回流。

    ``userId`` 不传时从会话主表反查——与 Redis 过期监听器的行为一致。

    Note:
        这里**直接 await**，返回时回流已经跑完，``flushed`` 字段是真实结果；
        回流失败会记录到日志 ``[LongMem] 回流 userId=... session=...``。
    """
    uid = user_id if user_id is not None else await session_service.find_user_id(session_id)
    if uid is None:
        return {"ok": False, "reason": "无法解析 userId，请显式传 userId 参数"}

    flushed = await writer.flush_on_session_end(session_id, uid)
    return {"ok": True, "sessionId": session_id, "userId": uid, "flushed": flushed}


@router.post("/trim")
async def trim(
    guard: Annotated[AgentMemoryGuard, Depends(get_agent_memory_guard)],
) -> dict:
    """手工触发一次 Agent 记忆巡检（对应 ``MemoryGuardDebugController``）。

    正常情况下一分钟自动跑一次，这个入口是给"压测完立刻验收"用的——
    不然要盯着日志等一分钟。

    Note:
        返回的 ``trimmed`` 是**被清空的会话数**，不是扫描数。``0`` 是正常结果，
        说明水位策略工作正常、没有哪条记忆涨过 40 条——**不是"没跑"**。
    """
    return {"status": "ok", "trimmed": await guard.trim_all()}
