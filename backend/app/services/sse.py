"""进程内 SSE hub。

- case_id → set[asyncio.Queue]：同一案件可多端订阅；
- emit(case_id, event, data, persist=True)：persist 时先落 messages（案件内 seq 单调递增，
  作为 SSE 的 id / Last-Event-ID），再广播给所有订阅队列；
- subscribe()：async 上下文管理器，进出自动注册/注销队列；
- replay(case_id, after_seq)：从 messages 表读 seq > after_seq 的历史，重放为 SSE 事件。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from ulid import ULID

from ..db import database as db

logger = logging.getLogger(__name__)


def _persist_message_sync(
    case_id: str, event: str, data: Any, step_key: str | None, role: str
) -> int:
    """落 messages 表（seq 分配 + 插入在同一事务内），返回分配到的 seq。"""

    def op(conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM messages WHERE case_id=?", (case_id,)
        ).fetchone()
        seq = int(row[0]) + 1
        content = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        meta = json.dumps({"event": event}, ensure_ascii=False)
        conn.execute(
            """
            INSERT INTO messages(id, case_id, seq, role, step_key, content, meta_json, created_at)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (str(ULID()), case_id, seq, role, step_key, content, meta, db.now_str()),
        )
        return seq

    return db.transaction(op)


def _row_to_sse(row: sqlite3.Row) -> dict[str, Any]:
    """messages 行 → sse-starlette 事件 dict（id=seq，event 取 meta_json.event）。"""
    event = "message"
    if row["meta_json"]:
        try:
            event = json.loads(row["meta_json"]).get("event") or "message"
        except json.JSONDecodeError:
            pass
    return {"event": event, "data": row["content"], "id": str(row["seq"])}


class SseHub:
    """进程内发布/订阅中心（单进程 FastAPI 专用）。"""

    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue]] = {}

    async def emit(
        self,
        case_id: str,
        event: str,
        data: Any,
        *,
        persist: bool = True,
        step_key: str | None = None,
        role: str = "event",
    ) -> int | None:
        """发布事件。persist=True 时先落 messages（返回 seq），再广播给订阅者。"""
        seq: int | None = None
        if persist:
            seq = await db.arun(_persist_message_sync, case_id, event, data, step_key, role)
        payload = {
            "event": event,
            "data": data if isinstance(data, str) else json.dumps(data, ensure_ascii=False),
            "id": str(seq) if seq is not None else None,
        }
        for queue in list(self._subs.get(case_id, ())):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:  # 理论上无界队列不会满；防御性忽略
                logger.warning("SSE 队列已满，丢弃事件 case=%s event=%s", case_id, event)
        return seq

    @asynccontextmanager
    async def subscribe(self, case_id: str) -> AsyncIterator[asyncio.Queue]:
        """订阅某案件的事件队列；退出上下文自动注销。"""
        queue: asyncio.Queue = asyncio.Queue()
        self._subs.setdefault(case_id, set()).add(queue)
        try:
            yield queue
        finally:
            subs = self._subs.get(case_id)
            if subs is not None:
                subs.discard(queue)
                if not subs:
                    self._subs.pop(case_id, None)

    def subscriber_count(self, case_id: str) -> int:
        """当前订阅者数量（调试用）。"""
        return len(self._subs.get(case_id, ()))


async def replay(case_id: str, after_seq: int) -> list[dict[str, Any]]:
    """读取 seq > after_seq 的历史消息，转为 SSE 事件列表（Last-Event-ID 重放）。"""
    rows = await db.aquery_all(
        "SELECT * FROM messages WHERE case_id=? AND seq>? ORDER BY seq ASC",
        (case_id, after_seq),
    )
    return [_row_to_sse(r) for r in rows]


# 全局单例
hub = SseHub()
