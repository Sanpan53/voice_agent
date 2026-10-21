"""Agent 内部记忆的定时巡检兜底。

由后台任务按固定周期触发。

**这是兜底，不是主力。** 主力是 :class:`~app.agent.agent_memory_policy.AgentMemoryPolicy`
那三条**调用前裁剪**（意图全清、推荐 8 条、情感 40 条）——正常路径下，
每次 ``agent(...)`` 之前都会裁一遍，内存涨不起来。

那为什么还要巡检？因为"每次调用前都裁"这个前提**依赖于每一次调用都记得裁**。
将来加一条新的 Agent 调用路径、或者某条分支提前 return 跳过了裁剪，
历史就会在某个长会话里悄悄涨到几百条——**Token 账单会涨，但不会有任何报错**。
巡检每分钟扫一遍所有活跃会话，把超限的强制清掉，相当于给那三条策略买了个保险。

.. note::

   **先取快照，再放锁外回调。** ``memory.clear()`` 是 **async**，而
   ``threading.Lock`` **绝不能跨 ``await`` 持有**（会阻塞整个事件循环上的
   其它协程去抢这把锁，而且是死锁形态的卡顿，不是异常）。所以
   :meth:`AgentFactory.for_each_session` 改成"在锁内取一份快照、放锁外再
   逐个等回调"。

   代价是：遍历期间新 ``get()`` 进来的会话这一轮扫不到。可以接受——
   它下一分钟会被扫到，而且**新会话的记忆本来就是空的**。
"""

import asyncio
import contextlib
import logging

from app.agent.agent_factory import AgentFactory, AgentSet
from app.agent.agent_memory_policy import EMOTION_MEMORY_LIMIT

logger = logging.getLogger(__name__)

#: 巡检周期（秒）。
DEFAULT_INTERVAL_SECONDS = 60


class AgentMemoryGuard:
    """定时把所有活跃会话的 Agent 记忆裁回水位以内。

    Args:
        factory: Agent 工厂（要能枚举出所有活跃会话）。
        limit: 水位。默认与 :data:`~app.agent.agent_memory_policy.EMOTION_MEMORY_LIMIT`
            取同一个值——**巡检的对象就是情感 Agent 那条最长的记忆**，
            Intent 每轮清空、Recommend 只有 8 条，都够不着 40。
        interval_seconds: 巡检周期，默认 60 秒。

    Note:
        水位与调用前裁剪用的是**同一个常量**。如果两处各写一个阈值，
        改一处忘另一处不会报错，只会让巡检要么永远不触发、要么每轮都在清。
    """

    def __init__(
        self,
        factory: AgentFactory,
        limit: int = EMOTION_MEMORY_LIMIT,
        interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    ) -> None:
        self._factory = factory
        self._limit = limit
        self._interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None

    async def trim_all(self) -> int:
        """扫一遍所有活跃会话，返回**被清空的会话数**。

        返回的是清理数而不是扫描数：调试接口和日志要回答的问题是
        "这次清理有没有干活"，而扫描数恒定等于活跃会话数，没有信息量。
        """
        trimmed = 0

        async def _inspect(session_id: str, agent_set: AgentSet) -> None:
            nonlocal trimmed
            memory = getattr(agent_set.emotion, "memory", None)
            if memory is None:
                return
            size = await memory.size()
            if size <= self._limit:
                return
            await memory.clear()
            trimmed += 1
            logger.warning("[Cost] memory 超长清空 sessionId=%s size=%d", session_id, size)

        await self._factory.for_each_session(_inspect)
        return trimmed

    # ------------------------------------------------------------------
    # 定时循环
    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        """定时循环体。由 lifespan 起一个 task 跑，关闭时 cancel。

        单次巡检抛异常**不能让循环退出**——一次数据库抖动、一个已经被清掉的
        会话，不该让"每分钟扫一遍"变成"只扫过一遍"。
        """
        while True:
            await asyncio.sleep(self._interval_seconds)
            try:
                trimmed = await self.trim_all()
                if trimmed:
                    logger.info("[Cost] 记忆巡检清理了 %d 个会话", trimmed)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — 巡检失败只记日志，绝不退出循环
                logger.error("[Cost] 记忆巡检失败", exc_info=True)

    def start(self) -> None:
        """启动后台巡检任务（幂等：已经在跑就不再起第二个）。"""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self.run_forever())
        logger.info("[Cost] 记忆巡检已启动，周期 %d 秒", self._interval_seconds)

    async def stop(self) -> None:
        """停止巡检任务。应用关闭时调用。"""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    @property
    def running(self) -> bool:
        """巡检任务是否在跑（测试 / 调试用）。"""
        return self._task is not None and not self._task.done()
