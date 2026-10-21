"""FastAPI 应用生命周期。

应用启动与周期任务。**关键设计**：启动钩子抛异常如果让**整个应用起不来**，代价太大；
Python 这边的启动动作（记忆巡检、TTS 短语预热）都是**成本优化项**——它们失败最多让这一轮贵一点，
不该有拦下整个服务的权力。所以下面每一段都自己吞异常、只记日志。
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.infrastructure.logging import configure_logging
from app.infrastructure.redis import close_redis
from app.security.current_user import _warn_if_open

logger = logging.getLogger(__name__)


async def _start_memory_guard() -> None:
    """启动 Agent 记忆巡检。

    纯 asyncio 循环，不碰网络、不碰数据库，**没有失败的理由**——
    真失败了（比如工厂构造就抛了）也只记日志，不挡启动。
    """
    try:
        from app.api.dependencies import get_agent_memory_guard

        get_agent_memory_guard().start()
    except Exception:  # noqa: BLE001 — 巡检是兜底，起不来不该拦住服务
        logger.error("[Cost] 记忆巡检启动失败", exc_info=True)


async def _stop_memory_guard() -> None:
    """停掉巡检任务，避免关闭时留下悬挂的 task。"""
    try:
        from app.api.dependencies import get_agent_memory_guard

        await get_agent_memory_guard().stop()
    except Exception:  # noqa: BLE001
        logger.error("[Cost] 记忆巡检停止失败", exc_info=True)


async def _preload_tts_phrases() -> None:
    """按需预热 TTS 短语缓存。

    **默认不跑。** 预热会真的调 DashScope——那边抖一下就会抛异常，
    如果无条件执行且不吞异常，整个应用都起不来。
    这里改成显式开关（``TTS_PHRASE_PRELOAD_ON_STARTUP=true``），
    并且 ``CachedTtsPhrases.preload`` 自己吞掉异常。
    要临时预热又不想重启，调 ``POST /api/v1/debug/tts-cache/preload``。
    """
    if not get_settings().tts_phrase_preload_on_startup:
        logger.info("[Cost] 跳过 TTS 短语预热（未开启 TTS_PHRASE_PRELOAD_ON_STARTUP）")
        return
    try:
        from app.api.dependencies import get_cached_tts_phrases

        await get_cached_tts_phrases().preload()
    except Exception:  # noqa: BLE001 — 预热失败只是没省到钱
        logger.error("[Cost] TTS 短语预热失败", exc_info=True)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """启动时配置日志 + 起成本控制的后台任务，关闭时依次收摊。"""
    configure_logging()
    # 认证开关关着就在启动日志里吵一句。
    # 必须在 configure_logging 之后调，否则那句 warning 会因为还没配 handler 而丢掉。
    _warn_if_open()

    await _start_memory_guard()
    await _preload_tts_phrases()

    yield

    await _stop_memory_guard()
    await close_redis()
