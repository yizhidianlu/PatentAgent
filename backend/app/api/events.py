"""SSE 事件流 API：GET /cases/{id}/events。

行为（backend-architecture.md §3.4）：
- 支持 Last-Event-ID（header 或 last_event_id 查询参数）：先从 messages 表重放其后的历史；
- 之后订阅进程内 hub 实时转发；
- 空闲 15 秒发送一次 ping 事件（不落库）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Query, Request
from sse_starlette.sse import EventSourceResponse

from ..db import database as db
from ..services import sse
from .deps import client_ip, current_user, resolve_case_sync

router = APIRouter(tags=["事件流"])

PING_INTERVAL = 15  # 秒


def _parse_last_event_id(raw: str | None) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


@router.get("/cases/{case_id}/events", summary="案件 SSE 事件流（Last-Event-ID 重放 + 15s ping）")
async def case_events(
    case_id: str,
    request: Request,
    last_event_id: str | None = Query(default=None, description="EventSource 无法带 header 时的替代参数"),
    user: dict[str, Any] = Depends(current_user),
):
    # 数据隔离：他人案件的事件流一律 404（SSE 也是一条取数入口）
    await db.arun(resolve_case_sync, case_id, user, ip=client_ip(request))

    after_seq = _parse_last_event_id(request.headers.get("last-event-id"))
    if after_seq is None:
        after_seq = _parse_last_event_id(last_event_id)

    async def gen() -> AsyncIterator[dict[str, Any]]:
        # 1) 历史重放（仅当客户端带了 Last-Event-ID）
        if after_seq is not None:
            for ev in await sse.replay(case_id, after_seq):
                yield ev
        # 2) 实时订阅 + 空闲 ping（断连由 EventSourceResponse 监听并取消本生成器）
        async with sse.hub.subscribe(case_id) as queue:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=PING_INTERVAL)
                    yield item
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": json.dumps({"t": db.now_str()}, ensure_ascii=False)}

    # 自带 ping 事件，库层注释 ping 周期调大即可
    return EventSourceResponse(gen(), ping=600)
