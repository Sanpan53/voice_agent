"""日志配置（对应原 logging 配置）。"""

import logging

from app.config import get_settings


def configure_logging() -> None:
    """按环境配置根日志：development 用 DEBUG，其余 INFO。"""
    level = logging.DEBUG if get_settings().app_env == "development" else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(threadName)s] %(levelname)-5s %(name)s - %(message)s",
    )
