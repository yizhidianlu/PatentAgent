"""SQLite 访问层。

设计要点（backend-architecture.md §0 决策 6）：
- stdlib sqlite3 + WAL 模式 + foreign_keys；
- 单一全局连接（check_same_thread=False）+ threading.Lock 串行化访问；
- 尝试加载 sqlite-vec 扩展（Windows/Py3.13 下允许失败，记录状态供 /system/env 上报）；
- 迁移 = 编号 SQL 文件（migrations/NNN_*.sql）+ schema_version 表；
- 通过 anyio.to_thread 提供异步包装 helper。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Callable, TypeVar

import anyio.to_thread

from ..config import get_config

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

T = TypeVar("T")

# 全局连接与写锁（本地单用户，无并发压力；读写统一走同一把锁最稳妥）
_conn: sqlite3.Connection | None = None
_lock = threading.RLock()

# sqlite-vec 扩展加载状态（供 /system/env 探测上报；加载失败不致命）
sqlite_vec_status: dict[str, Any] = {"loaded": False, "error": "尚未初始化", "version": None}


def now_str() -> str:
    """本地时间字符串，与 SQLite datetime('now','localtime') 同格式。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _try_load_sqlite_vec(conn: sqlite3.Connection) -> None:
    """尝试加载 sqlite-vec 扩展；失败仅记录，不抛出（M4 检索层会按状态降级为纯 Python 余弦）。"""
    global sqlite_vec_status
    try:
        import sqlite_vec  # 可选依赖：Windows/Py3.13 下可能装不上
    except Exception as exc:  # noqa: BLE001
        sqlite_vec_status = {"loaded": False, "error": f"sqlite-vec 包不可用: {exc}", "version": None}
        logger.warning("sqlite-vec 导入失败：%s", exc)
        return
    try:
        conn.enable_load_extension(True)
        try:
            sqlite_vec.load(conn)
        finally:
            conn.enable_load_extension(False)
        version = conn.execute("SELECT vec_version()").fetchone()[0]
        sqlite_vec_status = {"loaded": True, "error": None, "version": version}
        logger.info("sqlite-vec 加载成功，版本 %s", version)
    except Exception as exc:  # noqa: BLE001
        sqlite_vec_status = {"loaded": False, "error": f"扩展加载失败: {exc}", "version": None}
        logger.warning("sqlite-vec 扩展加载失败：%s", exc)


def get_conn() -> sqlite3.Connection:
    """取全局连接（懒创建；WAL + foreign_keys + 尝试加载 sqlite-vec）。"""
    global _conn
    with _lock:
        if _conn is None:
            cfg = get_config()
            cfg.ensure_dirs()
            conn = sqlite3.connect(str(cfg.db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            _try_load_sqlite_vec(conn)
            _conn = conn
        return _conn


def close_db() -> None:
    """关闭全局连接（测试/关停用）。"""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


# ---------------------------------------------------------------------------
# 迁移
# ---------------------------------------------------------------------------

def run_migrations() -> list[int]:
    """按编号顺序应用 migrations/NNN_*.sql，返回本次应用的版本号列表。"""
    applied: list[int] = []
    with _lock:
        conn = get_conn()
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        current = row[0] or 0
        files = sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))
        for path in files:
            version = int(path.name[:3])
            if version <= current:
                continue
            sql = path.read_text(encoding="utf-8")
            logger.info("应用数据库迁移 %s ...", path.name)
            # 迁移期间必须关外键。SQLite 改不了表约束，只能「建新表→复制→DROP 旧表」，
            # 而 DROP 会触发 ON DELETE CASCADE：实测重建 users 表把 sessions 与
            # usage_counters 一起清空了——用户被强制登出还只是麻烦，用量计数归零
            # 意味着配额失效、统计断档，而且不会有任何报错。
            # 关掉之后用 foreign_key_check 兜底，有悬空引用就回滚，不让脏数据落地。
            conn.commit()                      # PRAGMA foreign_keys 在事务内是空操作
            conn.execute("PRAGMA foreign_keys=OFF")
            try:
                conn.executescript(sql)
                violations = conn.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    conn.rollback()
                    raise RuntimeError(
                        f"迁移 {path.name} 产生了 {len(violations)} 处外键悬空引用，已回滚："
                        f"{violations[:5]}"
                    )
                conn.execute("DELETE FROM schema_version")
                conn.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))
                conn.commit()
            finally:
                conn.execute("PRAGMA foreign_keys=ON")
            applied.append(version)
            current = version
    if applied:
        logger.info("数据库迁移完成：%s", applied)
    return applied


def init_db() -> None:
    """初始化数据库：建目录、建连接、跑迁移（startup 时调用）。"""
    get_conn()
    run_migrations()


# ---------------------------------------------------------------------------
# 同步访问 helper（全部持锁）
# ---------------------------------------------------------------------------

def query_all(sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
    """查询多行。"""
    with _lock:
        return get_conn().execute(sql, params).fetchall()


def query_one(sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
    """查询单行。"""
    with _lock:
        return get_conn().execute(sql, params).fetchone()


def execute(sql: str, params: tuple | list = ()) -> int:
    """执行写语句并提交，返回受影响行数。"""
    with _lock:
        conn = get_conn()
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount


def transaction(fn: Callable[[sqlite3.Connection], T]) -> T:
    """在写锁内执行 fn(conn) 并提交；异常时回滚。用于需要原子性的多语句操作（如 seq 分配+插入）。"""
    with _lock:
        conn = get_conn()
        try:
            result = fn(conn)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise


# ---------------------------------------------------------------------------
# 异步包装（anyio.to_thread）
# ---------------------------------------------------------------------------

async def arun(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """把任意同步函数丢进线程池执行（位置参数与关键字参数均支持）。"""
    return await anyio.to_thread.run_sync(partial(fn, *args, **kwargs))


async def aquery_all(sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
    """query_all 的异步包装。"""
    return await anyio.to_thread.run_sync(partial(query_all, sql, params))


async def aquery_one(sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
    """query_one 的异步包装。"""
    return await anyio.to_thread.run_sync(partial(query_one, sql, params))


async def aexecute(sql: str, params: tuple | list = ()) -> int:
    """execute 的异步包装。"""
    return await anyio.to_thread.run_sync(partial(execute, sql, params))


async def atransaction(fn: Callable[[sqlite3.Connection], T]) -> T:
    """transaction 的异步包装。"""
    return await anyio.to_thread.run_sync(partial(transaction, fn))


# ---------------------------------------------------------------------------
# settings 表（key-value JSON）便捷读写
# ---------------------------------------------------------------------------

def get_setting_json(key: str) -> dict | None:
    """读 settings 表某 key 的 JSON 值；不存在返回 None。"""
    row = query_one("SELECT value_json FROM settings WHERE key=?", (key,))
    if row is None:
        return None
    try:
        return json.loads(row["value_json"])
    except (TypeError, json.JSONDecodeError):
        return None


def set_setting_json(key: str, value: dict) -> None:
    """写 settings 表某 key（UPSERT）。"""
    execute(
        """
        INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at
        """,
        (key, json.dumps(value, ensure_ascii=False), now_str()),
    )
