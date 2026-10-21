"""voice_agent FastAPI 启动入口。

启动 FastAPI 应用。
"""

from fastapi import FastAPI

from app.api import (
    agent_debug,
    auth,
    chat,
    cost_debug,
    event,
    faq,
    hub,
    memory_debug,
    orders,
    pages,
    product_index,
    product_search,
    profile,
    session,
    voice_ws,
)
from app.api.health import router as health_router
from app.config import get_settings
from app.lifespan import lifespan

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
)
app.include_router(health_router)
app.include_router(product_search.router)
app.include_router(product_index.router)
app.include_router(faq.router)
app.include_router(profile.router)
app.include_router(agent_debug.router)
app.include_router(hub.router)
app.include_router(event.router)
app.include_router(chat.router)
app.include_router(memory_debug.router)
app.include_router(cost_debug.router)
app.include_router(voice_ws.router)
app.include_router(auth.router)
app.include_router(session.router)
app.include_router(orders.router)
app.include_router(pages.router)
