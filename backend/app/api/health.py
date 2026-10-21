"""健康检查与 Prometheus 指标接口。"""

import time

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST

from app.config import get_settings
from app.infrastructure.metrics import render_metrics

router = APIRouter()


@router.get("/api/v1/ping", tags=["health"])
async def ping() -> dict[str, str | int]:
    """健康检查的路径与响应语义保持稳定。"""
    return {
        "app": get_settings().app_name,
        "status": "ok",
        "ts": int(time.time() * 1000),
    }


@router.get("/actuator/prometheus", tags=["monitoring"], include_in_schema=False)
async def prometheus() -> Response:
    """Prometheus 指标暴露端点。"""
    return Response(content=render_metrics(), media_type=CONTENT_TYPE_LATEST)
