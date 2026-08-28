"""案件与消息 API：列表 / 建案 / 单案读改删 / 会话历史。

数据隔离（auth-system.md §4.3）：建案写入 `user_id`；列表按
`deps.owned_case_ids_clause()` 过滤；单案一律经 `deps.resolve_case_sync()`
—— 普通用户命中他人案件返回 404，管理员只读放行并写 `cross_user_read` 审计。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from ulid import ULID

from ..config import get_config
from ..db import database as db
from ..models.artifact import ArtifactOut
from ..models.case import CaseCreate, CaseDetail, CaseOut, CaseUpdate, MessageOut
from ..models.common import Ok, Page
from ..services import auth as auth_service
from ..services import llm as llm_service
from .deps import client_ip, current_user, is_admin, resolve_case_sync

router = APIRouter(tags=["案件"])


def _parse_json(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _owner_brief_sync(user_id: str | None) -> tuple[str | None, str | None]:
    """取属主的用户名与显示名（供跨用户查看提示条展示）。"""
    if not user_id:
        return None, None
    row = db.query_one("SELECT username, display_name FROM users WHERE id=?", (user_id,))
    if not row:
        return None, None
    return row["username"], (row["display_name"] or row["username"])


def _row_to_case(row: sqlite3.Row, *, with_owner: bool = False) -> CaseOut:
    """DB 行 → CaseOut；with_owner 时补属主用户名（列表页逐行查库不划算，默认关）。"""
    owner_id = row["user_id"] if "user_id" in row.keys() else None
    username = display = None
    if with_owner:
        username, display = _owner_brief_sync(owner_id)
    return CaseOut(
        id=row["id"], module=row["module"], title=row["title"],
        patent_type=row["patent_type"], status=row["status"],
        current_step=row["current_step"], contact=_parse_json(row["contact_json"]),
        created_at=row["created_at"], updated_at=row["updated_at"],
        user_id=owner_id, owner_username=username, owner_display_name=display,
    )


def _row_to_message(row: sqlite3.Row) -> MessageOut:
    return MessageOut(
        id=row["id"], case_id=row["case_id"], seq=row["seq"], role=row["role"],
        step_key=row["step_key"], content=row["content"],
        meta=_parse_json(row["meta_json"]), created_at=row["created_at"],
    )


@router.get("/cases", response_model=Page[CaseOut], summary="案件列表（侧栏；支持 module/status/关键词过滤）")
async def list_cases(
    module: str | None = Query(default=None, description="按模块过滤"),
    status: str | None = Query(default=None, description="按状态过滤"),
    q: str | None = Query(default=None, description="标题关键词"),
    scope: str = Query(
        default="mine",
        pattern="^(mine|all)$",
        description="mine=只看自己的（默认）；all=看全部（仅管理员有效，用于管理后台）",
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: dict[str, Any] = Depends(current_user),
) -> Page[CaseOut]:
    where, params = ["1=1"], []
    if module:
        where.append("module=?")
        params.append(module)
    if status:
        where.append("status=?")
        params.append(status)
    if q:
        where.append("title LIKE ?")
        params.append(f"%{q}%")

    # 属主过滤。注意默认是 scope='mine'：管理员虽有权看全部，但侧栏默认
    # 只列自己的案件——否则用户一多，管理员的会话列表会被别人的案件淹没。
    # 要看全部请显式传 scope=all（管理后台用）。
    if scope == "all" and is_admin(user):
        owner_clause, owner_params = "", ()
    else:
        owner_clause, owner_params = " AND cases.user_id = ? ", (user["id"],)
    cond = f"WHERE {' AND '.join(where)}{owner_clause}"
    params = [*params, *owner_params]

    def op() -> tuple[list[sqlite3.Row], int]:
        rows = db.query_all(
            f"SELECT * FROM cases {cond} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        total_row = db.query_one(f"SELECT COUNT(*) AS n FROM cases {cond}", tuple(params))
        return rows, int(total_row["n"])

    rows, total = await db.arun(op)
    return Page(items=[_row_to_case(r) for r in rows], total=total, limit=limit, offset=offset)


@router.post("/cases", response_model=CaseOut, status_code=201, summary="新建案件（draft）")
async def create_case(
    body: CaseCreate, user: dict[str, Any] = Depends(current_user)
) -> CaseOut:
    case_id = str(ULID())
    now = db.now_str()
    title = (body.title or "").strip() or "未命名案件"

    def op() -> sqlite3.Row:
        # 档位：请求给了就用，没给就取设置里的默认档位。
        # 建案时就定下来，是为了让「首页选好档位再开跑」这条路不需要额外一次 PATCH——
        # 少一次往返，也少一个「PATCH 失败但案件已建」的中间态。
        tier = body.model_tier or llm_service.load_model_tiers().default_tier
        state = json.dumps({"_model_tier": tier}, ensure_ascii=False)
        db.execute(
            """
            INSERT INTO cases(id, module, title, patent_type, status, state_json,
                              user_id, created_at, updated_at)
            VALUES (?,?,?,?,'draft',?,?,?,?)
            """,
            (case_id, body.module, title, body.patent_type, state, user["id"], now, now),
        )
        # 属主刚由本请求写入，直接回读即可
        return db.query_one("SELECT * FROM cases WHERE id=?", (case_id,))

    row = await db.arun(op)
    return _row_to_case(row)


@router.get("/cases/{case_id}", response_model=CaseDetail,
            summary="单案详情：案件 + 流水线工作内存 + 各类交付物最新版")
async def get_case(
    case_id: str,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> CaseDetail:
    ip = client_ip(request)

    def op() -> tuple[dict[str, Any], list[sqlite3.Row]]:
        row = resolve_case_sync(case_id, user, ip=ip)
        latest = db.query_all(
            """
            SELECT a.* FROM artifacts a
            JOIN (SELECT kind, MAX(version) AS mv FROM artifacts WHERE case_id=? GROUP BY kind) m
              ON a.kind=m.kind AND a.version=m.mv
            WHERE a.case_id=?
            ORDER BY a.created_at DESC
            """,
            (case_id, case_id),
        )
        return row, latest

    row, latest = await db.arun(op)
    base = await db.arun(_row_to_case, row, with_owner=True)
    return CaseDetail(
        **base.model_dump(),
        state=_parse_json(row["state_json"]) or {},
        artifacts_latest=[ArtifactOut(**{**dict(a)}) for a in latest],
    )


@router.patch("/cases/{case_id}", response_model=CaseOut, summary="改名 / 归档 / 联系人 / 专利类型")
async def update_case(
    case_id: str,
    body: CaseUpdate,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> CaseOut:
    ip = client_ip(request)

    def op() -> sqlite3.Row:
        # write=True：管理员可以查看他人案件，但不得篡改其内容（→ 403）
        resolve_case_sync(case_id, user, ip=ip, write=True)
        sets, params = [], []
        if body.title is not None and body.title.strip():
            sets.append("title=?")
            params.append(body.title.strip())
        if body.status is not None:
            sets.append("status=?")
            params.append(body.status)
        if body.patent_type is not None:
            sets.append("patent_type=?")
            params.append(body.patent_type)
        if body.contact is not None:
            sets.append("contact_json=?")
            params.append(json.dumps(body.contact.model_dump(), ensure_ascii=False))
        if body.model_tier is not None:
            # 档位存进 state_json：它是流水线的运行参数，与 _run_group / _steps 同类，
            # 不值得为它加一列（也就不必为它做一次迁移）。
            state = _parse_json(db.query_one(
                "SELECT state_json FROM cases WHERE id=?", (case_id,)
            )["state_json"]) or {}
            state["_model_tier"] = body.model_tier
            sets.append("state_json=?")
            params.append(json.dumps(state, ensure_ascii=False))
        if sets:
            sets.append("updated_at=?")
            params.append(db.now_str())
            db.execute(f"UPDATE cases SET {', '.join(sets)} WHERE id=?", (*params, case_id))
        # 上面 resolve_case_sync(write=True) 已完成属主校验，这里只是回读更新后的行
        return db.query_one("SELECT * FROM cases WHERE id=?", (case_id,))

    row = await db.arun(op)
    return _row_to_case(row)


@router.delete("/cases/{case_id}", response_model=Ok, summary="删除案件（级联；purge_files=true 时连带清理磁盘文件）")
async def delete_case(
    case_id: str,
    request: Request,
    purge_files: bool = Query(default=False),
    user: dict[str, Any] = Depends(current_user),
) -> Ok:
    ip = client_ip(request)

    def op() -> dict[str, Any]:
        # 删除按只读口径解析：管理员可以删他人案件（§4.2），但必须留审计
        case = resolve_case_sync(case_id, user, ip=ip)
        db.execute("DELETE FROM cases WHERE id=?", (case_id,))
        if purge_files:
            cfg = get_config()
            for base in (cfg.uploads_dir, cfg.outputs_dir):
                shutil.rmtree(base / case_id, ignore_errors=True)
        return case

    case = await db.arun(op)
    await db.arun(
        auth_service.audit, "case_delete",
        actor_id=user["id"], actor_name=user.get("username"),
        target_type="case", target_id=case_id, target_owner=case.get("user_id"),
        detail={"title": case.get("title"), "module": case.get("module"),
                "purge_files": purge_files},
        ip=ip,
    )
    return Ok()


@router.get("/cases/{case_id}/messages", response_model=list[MessageOut],
            summary="会话历史（SSE 重放兜底；after_seq 之后）")
async def list_messages(
    case_id: str,
    request: Request,
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=2000),
    user: dict[str, Any] = Depends(current_user),
) -> list[MessageOut]:
    ip = client_ip(request)

    def op() -> list[sqlite3.Row]:
        resolve_case_sync(case_id, user, ip=ip)
        return db.query_all(
            "SELECT * FROM messages WHERE case_id=? AND seq>? ORDER BY seq ASC LIMIT ?",
            (case_id, after_seq, limit),
        )

    rows = await db.arun(op)
    return [_row_to_message(r) for r in rows]
