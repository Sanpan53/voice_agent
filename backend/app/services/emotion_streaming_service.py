"""情感 Agent 的流式版本。

**不等它全部生成完再说话**——模型吐第一个字就往下走，后面接
:class:`~app.voice.sentence_aggregator.SentenceAggregator` 攒成短句送 TTS。
这是把端到端首字从 2.9s 压到 1.2s 的第二刀（第一刀是合并理由与情感）。

.. important::

   ``ReActAgent`` 只有 ``reply()``／``__call__()``，返回的是**最终那一条** ``Msg``；
   它内部确实在逐块消费模型的流（``_react_agent.py`` 里那句
   ``async for content_chunk in res``），但**没有把流暴露出来**。

   流式出口是 ``AgentBase.set_msg_queue_enabled(True, queue)``：开启后
   Agent 每次 ``print`` 都会把 ``(msg, last, speech)`` 压进这个队列，
   而流式推理分支**每收到一个块就 print 一次**，因此可以用它来搭 Token 通道。

   **末帧会重复全文**——``last=True`` 那条带着累积完成的整段文本；
   ReActAgent 在 reasoning 跑完后会把完整 message 再吐一次。
   去重逻辑见 :func:`next_delta`。
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from agentscope.message import Msg

from app.agent.agent_factory import AgentFactory
from app.agent.agent_memory_policy import AgentMemoryPolicy
from app.schemas.recommend import RecommendResult
from app.services.emotion_service import build_products_payload
from app.services.session_mood_detector import SessionMoodDetector

logger = logging.getLogger(__name__)

#: 从队列里取下一帧时的等待上限（秒）。
#:
#: 没有它，Agent 在**一个块都没吐之前**就抛异常的话，消费者会永远等在
#: ``queue.get()`` 上——队列不会再有东西进来，而它并不知道生产者已经死了。
#: 这是超时值，不是延迟预算：正常一轮 Token 间隔在几十毫秒量级。
POLL_TIMEOUT_SECONDS = 30.0


def next_delta(accumulated: str, current: str) -> tuple[str, str]:
    """算出这一帧真正新增的部分。返回 ``(delta, 新的累积值)``。

    四种情况：

    ============================================  ==================  ==========
    情况                                           delta               新累积
    ============================================  ==================  ==========
    ``current`` 是 ``accumulated`` 的延长（正常增量） 多出来的那截        ``current``
    ``current == accumulated``（原样重放）          空                   不变
    ``accumulated`` 以 ``current`` 结尾（尾段重放）  空                   不变
    其余（模型换了说法）                             ``current``         追加
    ============================================  ==================  ==========

    第一行才是常态——DashScope 的流式响应**每一帧带的都是累积全文**
    （``_dashscope_model.py`` 里的 ``acc_content += message.content``），
    不是增量。把它当增量直接播，用户会听到"别、别纠、别纠结、别纠结，"。

    .. warning::

       第二、三行是**启发式**，代价是把"用户真想听的重叠字"也一起吞掉：
       模型如果连着说两个"哈哈"，第二帧 ``current == accumulated`` 会被当重放丢掉。
       这是刻意的启发式——真出问题再换成"按 ``last`` 标志
       区分重放"，那需要 AgentScope 给出更明确的信号。
    """
    if not current:
        return "", accumulated
    if current == accumulated or (accumulated and accumulated.endswith(current)):
        return "", accumulated
    if accumulated and current.startswith(accumulated):
        return current[len(accumulated) :], current
    return current, accumulated + current


def _text_of(msg: Msg) -> str:
    """取出 Msg 里的**正文**文本，跳过 thinking 块。

    只要给用户听的那部分。AgentScope 把思考放在独立的 ``"thinking"`` 块里，
    把它一起念出来等于把内心独白播给用户听。
    """
    return "".join(
        block["text"]
        for block in msg.get_content_blocks()
        if block["type"] == "text"
    )


class EmotionStreamingService:
    """情感 Agent 的 Token 级流式包装。"""

    def __init__(
        self,
        factory: AgentFactory,
        mood_detector: SessionMoodDetector,
        memory_policy: AgentMemoryPolicy | None = None,
    ) -> None:
        self._factory = factory
        self._mood_detector = mood_detector
        self._memory_policy = memory_policy or AgentMemoryPolicy()

    async def stream_wrap(
        self,
        session_id: str,
        user_utterance: str,
        user_needs: str,
        rec: RecommendResult,
    ) -> AsyncIterator[str]:
        """流式产出 Token 增量。空增量不会 yield。

        Raises:
            Exception: Agent 调用失败时**原样抛出**。调用方（Orchestrator）
                需要知道"这句没说完"，才能决定是补兜底话术还是就此打住。
                默默返回空流会让用户听到一片静音，且没有任何日志指向原因。

        Note:
            入参比底层 Agent 调用多一个 ``user_needs``——**不能把用户原话当需求描述
            用**，那是两回事。调用方手边就有槽位，这里按正常语义传真实槽位。
        """
        mood = await self._mood_detector.detect(session_id, user_utterance)
        payload: dict[str, Any] = {
            "userUtterance": user_utterance,
            "sessionMood": str(mood),
            "userNeeds": user_needs,
            "scene": rec.explanation_tone,
            "products": build_products_payload(rec),
        }

        agent = self._factory.get(session_id).emotion
        await self._memory_policy.before_emotion_call(agent)

        queue: asyncio.Queue = asyncio.Queue()
        agent.set_msg_queue_enabled(True, queue)
        task = asyncio.create_task(
            agent(Msg("user", json.dumps(payload, ensure_ascii=False), "user"))
        )

        try:
            async for delta in _drain(queue, task):
                yield delta
        finally:
            # **必须关掉**：Agent 是每会话缓存复用的（AgentFactory 的 LRU），
            # 留着队列等于给这个会话永久挂一个没人消费的 asyncio.Queue，
            # 下一轮的非流式调用（比如点评团、或者 handle() 分支）就会往里灌数据。
            agent.set_msg_queue_enabled(False)
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task


async def _abandon(future: "asyncio.Future[Any]") -> None:
    """取消一个还没完成的 ``queue.get()`` 并等它真正退出。

    直接丢掉 future 会留下一条 "Task was destroyed but it is pending!" 的告警，
    而且那个 get 有可能真的吞掉队列里的一帧。
    """
    future.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await future


async def _drain(queue: asyncio.Queue, task: "asyncio.Task[Any]") -> AsyncIterator[str]:
    """把 Agent 的消息队列翻译成 Token 增量流。

    队列里每一条都是 ``(msg, last, speech)``（见 ``AgentBase.print``）。消费循环
    要同时盯着**队列**和**Agent 任务**：

    - 队列先动 → 算增量、发出去，继续等；
    - Agent 任务先结束（正常结束或抛异常）→ 把队列里剩下的排空，然后 ``await task``
      让异常原样冒出去。

    只盯队列是不行的：Agent 在**一个块都没吐之前**就抛异常的话，队列从此静默，
    消费者会挂到天荒地老。因此必须同时盯着 Agent 任务，``asyncio.Queue``
    本身没有错误传播。
    """
    accumulated = ""
    getter: asyncio.Future[Any] = asyncio.ensure_future(queue.get())

    def take(item: Any) -> str:
        """把一条队列消息变成增量，并推进累积值。"""
        nonlocal accumulated
        msg, _last, _speech = item
        delta, accumulated = next_delta(accumulated, _text_of(msg))
        return delta

    while True:
        done, _ = await asyncio.wait(
            {getter, task},
            timeout=POLL_TIMEOUT_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )

        if not done:
            # 超时：队列和 Agent 双双静止。收掉待取的 future 再抛，
            # 免得留一条悬挂的 Task 在事件循环里。
            await _abandon(getter)
            raise TimeoutError(f"EmotionAgent 流式输出 {POLL_TIMEOUT_SECONDS}s 无任何响应")

        if getter in done:
            delta = take(getter.result())
            getter = asyncio.ensure_future(queue.get())
            if delta:
                yield delta
            continue

        # 只剩「Agent 任务结束」这一种可能。
        #
        # 但 ``asyncio.wait`` 只保证"``done`` 里的完成了"，不保证"没别的也刚完成"——
        # getter 完全可能在同一瞬间取到了最后一帧。所以**先看它活了没**，
        # 直接 cancel 会把那一帧连同 future 一起丢掉（末尾一两个字就此消失）。
        break

    if getter.done() and not getter.cancelled():
        delta = take(getter.result())
        if delta:
            yield delta
    else:
        await _abandon(getter)

    # Agent 完全可能"put 完就 return"，这时队列里还压着没取的帧。
    while not queue.empty():
        delta = take(queue.get_nowait())
        if delta:
            yield delta

    # 异常在这里原样抛出；正常结束时只是把结果丢掉（我们不需要最终 Msg）。
    await task
