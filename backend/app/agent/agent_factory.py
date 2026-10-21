"""按会话创建 Agent 实例的工厂。

ReActAgent 是**有状态**的（``InMemoryMemory`` 会记住这个 Agent 参与过的所有
Msg），语音导购又是多用户并发的，所以每个会话必须独立一套 Agent 实例。

两个容易混淆的语义，这里用两个方法名区分开：

* :meth:`AgentFactory.get` —— 主链路 4 个 Agent，**进 LRU 缓存**、按会话复用；
* :meth:`AgentFactory.new_perspective_team` —— 多视角点评团，**每次新建**、
  不进缓存。点评团走 MsgHub 旁路、生命周期只有一次讨论那么长，复用实例会让
  ``InMemoryMemory`` 累积前几轮内容，污染下次点评的上下文。
"""

import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from agentscope.agent import ReActAgent

#: 不带参数的 Agent 构造器（意图 / 澄清 / 推荐 / 情感）
AgentBuilder = Callable[[], ReActAgent]

#: 带 (name, prompt_path) 的 Agent 构造器（点评团）
PerspectiveAgentBuilder = Callable[[str, str], ReActAgent]


@dataclass(frozen=True)
class AgentSet:
    """一个会话的主链路 Agent 组。"""

    intent: ReActAgent
    clarify: ReActAgent
    recommend: ReActAgent
    emotion: ReActAgent


@dataclass(frozen=True)
class PerspectiveTeam:
    """一次讨论专用的三视角点评团。"""

    price_advisor: ReActAgent
    pro_runner: ReActAgent
    beginner_buyer: ReActAgent


class AgentFactory:
    """按 sessionId 创建并缓存 Agent 实例。

    Args:
        intent_builder: 意图 Agent 构造器。
        clarify_builder: 澄清 Agent 构造器。
        recommend_builder: 推荐 Agent 构造器。
        emotion_builder: 情感 Agent 构造器。
        perspective_builder: 点评团构造器，接收 ``(name, prompt_path)``。

    Note:
        传入构造器而不是具体类，是为了让工厂不依赖五个 Builder 的具体实现——
        它们分别在意图、澄清、推荐、情感、点评团各模块里逐个实现，也便于
        测试时替换成桩实现。
    """

    #: LRU 上限：够一千个活跃会话，量更大时换 cachetools 等库
    MAX_SESSIONS = 1000

    def __init__(
        self,
        intent_builder: AgentBuilder,
        clarify_builder: AgentBuilder,
        recommend_builder: AgentBuilder,
        emotion_builder: AgentBuilder,
        perspective_builder: PerspectiveAgentBuilder,
    ) -> None:
        self._intent_builder = intent_builder
        self._clarify_builder = clarify_builder
        self._recommend_builder = recommend_builder
        self._emotion_builder = emotion_builder
        self._perspective_builder = perspective_builder

        # 用 OrderedDict + move_to_end + popitem(last=False) 实现 LRU：
        # 访问时把会话移到队尾，超限时从队首淘汰。
        self._cache: OrderedDict[str, AgentSet] = OrderedDict()
        # 语音导购是多用户并发，缓存必须线程安全。
        self._lock = threading.Lock()

    def get(self, session_id: str) -> AgentSet:
        """取出（必要时创建）某个会话的 Agent 组。

        命中缓存会把该会话移到 LRU 队尾；超出 :attr:`MAX_SESSIONS` 时淘汰
        最久未使用的会话。
        """
        with self._lock:
            cached = self._cache.get(session_id)
            if cached is not None:
                self._cache.move_to_end(session_id)
                return cached

            agent_set = AgentSet(
                intent=self._intent_builder(),
                clarify=self._clarify_builder(),
                recommend=self._recommend_builder(),
                emotion=self._emotion_builder(),
            )
            self._cache[session_id] = agent_set
            while len(self._cache) > self.MAX_SESSIONS:
                self._cache.popitem(last=False)
            return agent_set

    def remove(self, session_id: str) -> None:
        """会话结束时主动清理。

        不清理的话，Agent 里的 ``InMemoryMemory`` 会持续持有历史，占内存。
        """
        with self._lock:
            self._cache.pop(session_id, None)

    def cached_session_count(self) -> int:
        """当前缓存的会话数（观测 / 测试用）。"""
        with self._lock:
            return len(self._cache)

    async def for_each_session(
        self, consumer: Callable[[str, AgentSet], Awaitable[None]]
    ) -> int:
        """遍历所有活跃会话，逐个 ``await consumer(session_id, agent_set)``。

        :class:`~app.agent.agent_memory_guard.AgentMemoryGuard`
        用它做记忆巡检。

        Args:
            consumer: 异步回调。

        Returns:
            本轮遍历到的会话数。

        .. important::

           **先在锁内取快照，再放锁外逐个 await。** ``memory.clear()`` /
           ``memory.size()`` 都是 **async**，而 ``threading.Lock`` 跨 ``await``
           持有会把这个线程堵死，事件循环上其它想拿同一把锁的协程（也就是
           **所有并发会话的 ``get()``**）全部卡住——是卡顿，不是异常，
           最难查的那种。

           代价：遍历期间新 ``get()`` 进来的会话这轮扫不到。可接受——
           新会话的记忆本来就是空的，而且它下一轮会被扫到。

           ``AgentSet`` 是 frozen dataclass、只装 Agent 引用，所以快照里的
           元素不会"失效"；最坏情况是某个会话在遍历途中被 :meth:`remove`，
           此时我们多清了一次它的记忆——无害。
        """
        with self._lock:
            snapshot = list(self._cache.items())
        for session_id, agent_set in snapshot:
            await consumer(session_id, agent_set)
        return len(snapshot)

    def new_perspective_team(self) -> PerspectiveTeam:
        """每次调用生成一套全新的「多视角点评团」三人组，不做缓存。"""
        return PerspectiveTeam(
            price_advisor=self._perspective_builder(
                "price_advisor", "prompts/perspective_price.txt"
            ),
            pro_runner=self._perspective_builder(
                "pro_runner", "prompts/perspective_pro.txt"
            ),
            beginner_buyer=self._perspective_builder(
                "beginner_buyer", "prompts/perspective_beginner.txt"
            ),
        )
