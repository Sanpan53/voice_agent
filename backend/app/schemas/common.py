"""共享类型与序列化约定。

DTO 设计为不可变结构，JSON 字段为 camelCase。Python 侧字段统一用 snake_case，
通过 ``alias_generator`` 序列化回 camelCase，保证接口返回的 JSON 契约稳定。
"""

from pydantic import BaseModel, ConfigDict


def to_camel(snake: str) -> str:
    """snake_case -> camelCase（供 alias_generator 使用）。"""
    head, *tail = snake.split("_")
    return head + "".join(word.capitalize() for word in tail)


class CamelModel(BaseModel):
    """所有契约 DTO 的基类：字段 snake_case，序列化输出 camelCase。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")
