"""Prometheus 指标（对应原 Micrometer + Prometheus）。"""

from prometheus_client import generate_latest


def render_metrics() -> bytes:
    """输出 Prometheus 文本格式（给 /actuator/prometheus 端点）。"""
    return generate_latest()
