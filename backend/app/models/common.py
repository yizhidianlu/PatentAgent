"""通用 API 契约模型（分页等）。"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """分页结果。"""

    items: list[T]
    total: int
    limit: int
    offset: int


class Ok(BaseModel):
    """通用成功响应。"""

    ok: bool = True


class OkMessage(Ok):
    """带一句人话反馈的成功响应（改密、删号等需要向用户交代做了什么）。"""

    message: str = ""
