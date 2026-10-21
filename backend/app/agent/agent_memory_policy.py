"""Agent 内部记忆（``InMemoryMemory``）的清理策略。

**先分清"记忆"的三个层次**——这是最容易混的地方：

```
┌────────────────────────────────────────────────────┐
│ 长期记忆（跨会话，永久存）                          │
│   存哪：PostgreSQL user_profile_*                   │
│   谁读：推荐 Agent、情感 Agent                      │
│   谁写：会话结束时的行为回流、用户主动填写          │
├────────────────────────────────────────────────────┤
│ 会话短期记忆（本次会话）                            │
│   存哪：Redis List voice_agent:short_memory:{sessionId}      │
│   谁读：意图 Agent（历史上下文）、情感 Agent（情绪）│
│   谁写：Orchestrator 每轮追加                       │
├────────────────────────────────────────────────────┤
│ Agent 内部记忆（一次 Agent 实例的对话上下文）       │
│   存哪：InMemoryMemory（AgentScope 自带）           │
│   谁读写：Agent 自己 ← 本模块管的就是这一层         │
└────────────────────────────────────────────────────┘
```

**关键认知**：``InMemoryMemory`` **不是用户会话记忆**。它只是单个 Agent 实例
在多次调用之间的对话上下文。本项目里每个会话的 Agent 实例被 ``AgentFactory``
缓存，所以它恰好也能跨轮——但它的生命周期**等于 Agent 实例生命周期**，不是会话。

留着太多历史有两个问题：

1. Token 消耗线性增长；
2. 老历史干扰新判断（尤其意图 Agent——上一轮的 ``category`` 会串到这一轮的
   ``intent`` 里）。

所以策略是**意图每轮清空、推荐裁到 4 轮、情感保留 20 轮**。

Python 侧的三处 API 约束（都需要留意）：

+ ``memory.clear()`` 是 **async**，所以本模块三个方法全是 ``async def``。
+ ``delete(msg_ids)`` 按**消息 ID** 删。要"从头删 N 条"得先
  ``get_memory()`` 拿到前 N 条的 ``id``。
+ ``memory.size()`` 同样是 async。
"""

import logging

from agentscope.agent import ReActAgent

logger = logging.getLogger(__name__)

#: 推荐 Agent 水位：1 轮 ≈ 2 条（user + assistant），4 轮 = 8 条
RECOMMEND_MEMORY_LIMIT = 8

#: 情感 Agent 水位：情绪追踪需要更长上下文，40 条（约 20 轮）再裁
EMOTION_MEMORY_LIMIT = 40


class AgentMemoryPolicy:
    """三个 Agent 各自调用前的记忆裁剪策略。

    用法：在对应 Service 里、``agent(...)`` **之前**插一行调用。

    ===========================  ==========================  ============
    Service                      方法                        行为
    ===========================  ==========================  ============
    ``IntentService.classify``   :meth:`before_intent_call`   每轮清空
    ``RecommendReasonService``   :meth:`before_recommend_call` 裁到 8 条
    ``EmotionService.wrap``      :meth:`before_emotion_call`  裁到 40 条
    ===========================  ==========================  ============

    ``ClarifyService`` 没有单独挂——澄清 Agent 每轮本来就是拿着 slots 重新算，
    不依赖 memory 历史，不需要专门裁剪。
    """

    async def before_intent_call(self, agent: ReActAgent | None) -> None:
        """意图 Agent：每轮清空。

        意图识别靠 Prompt 里注入的"最近 3 轮摘要"拿上下文（见
        :meth:`~app.services.intent_service.IntentService._recent_history`），
        **不需要** Agent 自己的 memory。清空能防止老对话干扰当前判断。
        """
        memory = _memory_of(agent)
        if memory is None:
            return
        await memory.clear()

    async def before_recommend_call(self, agent: ReActAgent | None) -> None:
        """推荐 Agent：超过 4 轮裁到 4 轮。

        避免 Token 线性增长，也避免历史推荐污染新推荐
        （用户说"再看看别的"，不能因为上文老推同一款就继续推它）。
        """
        await self._trim_to_last(agent, RECOMMEND_MEMORY_LIMIT, "recommend")

    async def before_emotion_call(self, agent: ReActAgent | None) -> None:
        """情感 Agent：保留相对长的历史用于情绪连续性判断，超过 20 轮才裁。

        情绪是连续的——用户上一轮很烦躁、这一轮只是问个价格，情感 Agent 应该
        还带着"这人不耐烦"的判断，所以水位比推荐 Agent 高得多。
        """
        await self._trim_to_last(agent, EMOTION_MEMORY_LIMIT, "emotion")

    @staticmethod
    async def _trim_to_last(agent: ReActAgent | None, limit: int, tag: str) -> None:
        """把 Agent 内部记忆从头裁到只剩最后 ``limit`` 条。

        先取出前 ``remove_count`` 条的 id 再批量删（一次调用而不是逐条循环），
        因为 ``delete()`` 只认消息 ID。
        """
        memory = _memory_of(agent)
        if memory is None:
            return

        size = await memory.size()
        if size <= limit:
            return

        messages = await memory.get_memory()
        remove_ids = [msg.id for msg in messages[: size - limit]]
        await memory.delete(remove_ids)
        logger.info("[MemoryPolicy] %s agent trimmed %d -> %d", tag, size, limit)


def _memory_of(agent: ReActAgent | None):
    """取 Agent 的内部记忆；Agent 为 ``None`` 或没有 memory 时返回 ``None``。"""
    if agent is None:
        return None
    return getattr(agent, "memory", None)
