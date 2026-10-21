"""WebSocket 语音入口。

一个连接一条音频流：上行二进制帧喂给 ASR，ASR 的实时结果以 JSON 下发，
一句话说完时触发 Orchestrator 主链路，把下来的 :class:`~app.schemas.stream.StreamChunk`
翻译成三种帧推给前端：

======================  ==================================================
分片                     线格式
======================  ==================================================
``PRODUCTS``            ``{"type": "recommendation", "items": [...]}``
``TEXT``                ``{"type": "caption", "text": "..."}``
``AUDIO``               裸二进制帧（PCM 16k 16bit 单声道）
======================  ==================================================

**TTS 不在这一层做了**（原先在）。流式之后"什么时候该合成"由
Orchestrator 决定——它才知道一句话什么时候算完整，也才拿得到逐句的文字。
这一层退化成纯粹的"分片 → 帧"翻译器。

会话标识从 URL query 取（``?userId=1&sessionId=sess-xyz``），
由 ``websocket.query_params`` 直接读取。

**身份从哪来**：浏览器的 ``WebSocket`` 构造函数**不允许自定义
请求头**，所以 ``Authorization: Bearer`` 这条路走不通，只能把 token 放进
``Sec-WebSocket-Protocol`` 子协议里：``new WebSocket(url, ["Bearer-" + token])``。
服务端解析出来后，还得**在 accept 时把这个子协议原样回显**——不回显的话
浏览器会按 RFC 6455 判定"服务端没选中我提的任何子协议"并**直接断开连接**，
表现为"连上了又立刻掉线"，很难查。
"""

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from app.agent.agent_factory import AgentFactory
from app.api.dependencies import (
    get_agent_factory,
    get_asr_service,
    get_long_term_memory_writer,
    get_orchestrator_service,
    get_tts_service,
)
from app.schemas.stream import StreamChunk, StreamKind
from app.security.current_user import resolve_request_user_id
from app.security.tokens import SUBPROTOCOL_PREFIX, extract_token_from_subprotocol
from app.services.long_term_memory_writer import LongTermMemoryWriter
from app.services.orchestrator_service import OrchestratorService
from app.voice.asr_service import AsrService
from app.voice.tts_service import TtsService, single_text_stream

logger = logging.getLogger(__name__)

router = APIRouter()

#: 主链路整个挂掉时的兜底话术
FALLBACK_SPEECH = "鸡哥这边网络有点卡，你再说一次？"

#: 鉴权失败的关闭码：4400-4999 是应用自定义区间，4401 对应 HTTP 401
WS_UNAUTHORIZED = 4401


def resolve_session_id(websocket: WebSocket) -> str:
    """从 query 参数取 sessionId，没传就现生成一个。

    直接读 query，缺省补一个 UUID。
    """
    value = websocket.query_params.get("sessionId")
    return value if value else str(uuid.uuid4())


def resolve_user_id(websocket: WebSocket) -> int:
    """从 query 参数取 userId，解析不了就退化成 0。

    .. warning::

       **这个值只在认证未启用时才被采纳**，见 :func:`authenticate`。
       它保留下来是为了让本地跑前面的语音链路时不用先配密钥。
    """
    raw = websocket.query_params.get("userId")
    if raw is None:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning("userId 解析失败，退化为 0: %r", raw)
        return 0


def resolve_subprotocol(websocket: WebSocket) -> str | None:
    """取出客户端声明的 ``Bearer-<token>`` 子协议原文（用于 accept 时回显）。"""
    raw = websocket.headers.get("sec-websocket-protocol")
    if not raw:
        return None
    for candidate in (part.strip() for part in raw.split(",")):
        if candidate.startswith(SUBPROTOCOL_PREFIX):
            return candidate
    return None


def authenticate(websocket: WebSocket) -> tuple[int, str | None] | None:
    """握手鉴权。

    解析子协议里的 token、校验并解出 userId；校验不过则拒绝握手。

    Returns:
        ``(user_id, 需要回显的子协议)``；**认证开启但 token 无效时返回 ``None``**，
        调用方应当拒绝这个连接。子协议为 ``None`` 表示客户端没带 token
        （认证关着的情况），accept 时不回显。
    """
    subprotocol = resolve_subprotocol(websocket)
    token = extract_token_from_subprotocol(subprotocol)
    user_id = resolve_request_user_id(token, fallback_user_id=resolve_user_id(websocket))
    if user_id is None:
        return None
    return user_id, subprotocol


@dataclass(frozen=True)
class _Outgoing:
    """待下发的消息：ASR 文本 或 触发一次流式主链路的整句。"""

    kind: str  # "asr" | "utterance" | "close"
    text: str = ""
    final: bool = False


_CLOSE = _Outgoing(kind="close")


class _ConnectionClosed(Exception):
    """对端已断开——用来把下发循环从 ``_send_chunk`` 里拽出来。"""


async def _send_chunk(websocket: WebSocket, chunk: StreamChunk) -> None:
    """把一个 :class:`StreamChunk` 翻译成一帧推给前端。

    ``RuntimeError`` 也要接住：Starlette 在连接已关闭后再调 ``send_*`` 抛的是
    这个（"Cannot call send once a close message has been sent"），不是
    ``WebSocketDisconnect``。只接后者的话，前端一断线服务端就满屏堆栈。
    """
    try:
        if chunk.kind is StreamKind.PRODUCTS:
            await websocket.send_json(
                {
                    "type": "recommendation",
                    "items": [item.model_dump(by_alias=True) for item in chunk.products],
                }
            )
        elif chunk.kind is StreamKind.TEXT:
            await websocket.send_json({"type": "caption", "text": chunk.text})
        else:  # StreamKind.AUDIO — 裸二进制帧
            await websocket.send_bytes(chunk.audio)
    except (WebSocketDisconnect, RuntimeError) as exc:
        raise _ConnectionClosed from exc


async def _pump_outgoing(
    websocket: WebSocket,
    queue: "asyncio.Queue[_Outgoing]",
    tts: TtsService,
    orchestrator: OrchestratorService,
    session_id: str,
    user_id: int,
) -> None:
    """串行消费下发队列：先发 ASR 文本，再跑流式主链路并逐片下发。"""
    while True:
        item = await queue.get()
        if item.kind == "close":
            return

        if item.kind == "asr":
            # 前端可能已经关了连接（ASR 尾部结果晚于 close 回来），按正常残响处理。
            try:
                await websocket.send_json(
                    {"type": "asr", "text": item.text, "final": item.final}
                )
            except (WebSocketDisconnect, RuntimeError) as exc:
                logger.warning("下发 ASR 结果失败（连接可能已关闭）: %s", exc)
                return
            continue

        # item.kind == "utterance"
        emitted = False
        try:
            async for chunk in orchestrator.stream_handle(session_id, user_id, item.text):
                await _send_chunk(websocket, chunk)
                emitted = True
        except _ConnectionClosed as exc:
            logger.warning("下发流式分片失败（连接可能已关闭）: %s", exc)
            return
        except Exception:  # noqa: BLE001 — 主链路失败必须降级，不能拖垮连接
            logger.error("Orchestrator 流式处理失败 sessionId=%s", session_id, exc_info=True)
            # **已经出过声就不再补兜底**——用户听到 "…第一款是 Asics，鸡哥这边网络
            # 有点卡" 比直接停住更像故障。判据与 Orchestrator 内部
            # ``_stream_speech`` 保持一致：说了一半就让它停在半句。
            if not emitted:
                try:
                    async for frame in tts.synthesize(single_text_stream(FALLBACK_SPEECH)):
                        await websocket.send_bytes(frame)
                except Exception as exc:  # noqa: BLE001 — 降级也失败就只记 warn
                    logger.warning("降级音频下发失败 sessionId=%s: %s", session_id, exc)


@router.websocket("/ws/voice")
async def voice_endpoint(
    websocket: WebSocket,
    asr: Annotated[AsrService, Depends(get_asr_service)],
    tts: Annotated[TtsService, Depends(get_tts_service)],
    orchestrator: Annotated[OrchestratorService, Depends(get_orchestrator_service)],
    memory_writer: Annotated[
        LongTermMemoryWriter, Depends(get_long_term_memory_writer)
    ],
    agent_factory: Annotated[AgentFactory, Depends(get_agent_factory)],
) -> None:
    """浏览器 ↔ 服务端的实时语音通道。"""
    auth = authenticate(websocket)
    if auth is None:
        # WebSocket 没有 HTTP 状态码可用，
        # 用 4401（应用自定义码，语义上对应 HTTP 401）拒绝，前端据此提示"请重新登录"。
        await websocket.close(code=WS_UNAUTHORIZED)
        logger.warning("语音通道鉴权失败，已拒绝连接")
        return

    user_id, subprotocol = auth
    # **必须回显客户端提的子协议**，否则浏览器握手后立刻断开（见模块 docstring）。
    await websocket.accept(subprotocol=subprotocol)

    session_id = resolve_session_id(websocket)
    logger.info("语音通道已连接 sessionId=%s userId=%s", session_id, user_id)

    loop = asyncio.get_running_loop()
    outgoing: asyncio.Queue[_Outgoing] = asyncio.Queue()

    def on_partial(text: str, is_end: bool) -> None:
        """ASR 回调（SDK 线程触发，切回事件循环再入队）。"""
        loop.call_soon_threadsafe(
            outgoing.put_nowait, _Outgoing(kind="asr", text=text, final=is_end)
        )
        if is_end and text.strip():
            loop.call_soon_threadsafe(
                outgoing.put_nowait, _Outgoing(kind="utterance", text=text)
            )

    session = asr.create_session(on_partial)
    writer = asyncio.create_task(
        _pump_outgoing(websocket, outgoing, tts, orchestrator, session_id, user_id)
    )

    try:
        await session.start()
        while True:
            frame = await websocket.receive_bytes()
            await session.send_frame(frame)
    except WebSocketDisconnect:
        logger.debug("WebSocket 已断开")
    except Exception as exc:  # noqa: BLE001 — 连接级异常，记录后走清理
        logger.error("语音通道异常: %s", exc)
    finally:
        with contextlib.suppress(Exception):
            await session.stop()
        await outgoing.put(_CLOSE)
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(writer, timeout=5)

        # 触发点 1：连接关闭 → 长期记忆回流 + Agent 实例清理。
        # 回流必须放在 writer 任务收尾**之后**——主链路可能还在写短期记忆，
        # 提前回流会读到不完整的会话。
        await _flush_on_close(memory_writer, agent_factory, session_id, user_id)

        with contextlib.suppress(Exception):
            await websocket.close()


async def _flush_on_close(
    memory_writer: LongTermMemoryWriter,
    agent_factory: AgentFactory,
    session_id: str,
    user_id: int,
) -> None:
    """连接关闭时的收尾：回流长期记忆 + 清掉 Agent 缓存。

    两步都要吞异常——连接已经断了，这里再抛出去只会让 ASGI 打印一堆
    无意义的堆栈，掩盖真正的业务错误。

    ``agent_factory.remove`` 是为了让这个会话的 4 个 Agent 实例和它们的
    ``InMemoryMemory`` 尽快被 GC。不清理的话，LRU 缓存虽然最终会淘汰，
    但那之前这些 Agent 一直占着内存。
    """
    try:
        await memory_writer.flush_on_session_end(session_id, user_id)
    except Exception:  # noqa: BLE001 — 回流失败不能影响连接收尾
        logger.error("连接关闭时回流长期记忆失败 sessionId=%s", session_id, exc_info=True)

    try:
        agent_factory.remove(session_id)
    except Exception:  # noqa: BLE001
        logger.warning("清理 Agent 缓存失败 sessionId=%s", session_id, exc_info=True)
