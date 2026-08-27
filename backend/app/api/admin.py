"""管理后台 API：用户 CRUD / 重置密码 / 审计日志 / 平台统计（M8）。

对应 docs/design/auth-system.md §5.1 的 /admin/* 部分。整个 router 挂
require_admin 依赖，普通用户一律 403。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..db import database as db
from ..models.auth import (
    AdminStatsOut,
    AuditEntryOut,
    PasswordIssuedOut,
    UserCreateIn,
    UserOut,
    UserUpdateIn,
    RegistrationPolicyIn,
    RegistrationPolicyOut,
)
from ..models.common import OkMessage, Page
from ..services import auth as auth_service
from .deps import client_ip, require_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["管理后台"], dependencies=[Depends(require_admin)])


def _user_out(row: dict[str, Any], *, with_usage: bool = True) -> UserOut:
    usage = auth_service.usage_snapshot(row["id"]) if with_usage else None
    return auth_service._row_to_user(row, usage=usage)


def _admin_count_sync() -> int:
    row = db.query_one("SELECT COUNT(*) AS n FROM users WHERE role='admin' AND status='active'")
    return int(row["n"]) if row else 0


# ---------------------------------------------------------------------------
# 用户管理
# ---------------------------------------------------------------------------


@router.get("/registration", response_model=RegistrationPolicyOut, summary="自助注册策略")
async def get_registration(
    admin: dict[str, Any] = Depends(require_admin),
) -> RegistrationPolicyOut:
    def _run() -> RegistrationPolicyOut:
        return RegistrationPolicyOut(
            allow_registration=auth_service.registration_open(),
            pending_count=auth_service.count_pending(),
        )

    return await db.arun(_run)


@router.put("/registration", response_model=RegistrationPolicyOut, summary="开关自助注册")
async def set_registration(
    body: RegistrationPolicyIn,
    request: Request,
    admin: dict[str, Any] = Depends(require_admin),
) -> RegistrationPolicyOut:
    """关掉之后登录页不再显示注册入口，注册接口也直接拒绝。

    已经处于待审的账号不受影响——关闭注册是「不再收新的」，
    不是「把排队的人赶走」，那是两件事，混在一起会让管理员误伤。
    """
    def _run() -> RegistrationPolicyOut:
        auth_service.set_registration_open(body.allow_registration)
        auth_service.audit(
            "registration_policy_changed",
            actor_id=admin["id"], actor_name=admin["username"],
            detail={"allow_registration": body.allow_registration},
            ip=client_ip(request),
        )
        return RegistrationPolicyOut(
            allow_registration=auth_service.registration_open(),
            pending_count=auth_service.count_pending(),
        )

    return await db.arun(_run)


@router.get("/users", response_model=Page[UserOut], summary="用户列表")
async def list_users(
    q: str | None = Query(default=None, description="按用户名或显示名模糊搜索"),
    role: str | None = Query(default=None, pattern="^(admin|user)$"),
    status: str | None = Query(default=None, pattern="^(active|disabled|pending)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[UserOut]:
    def _run() -> Page[UserOut]:
        where, params = ["1=1"], []
        if q:
            where.append("(username LIKE ? OR display_name LIKE ?)")
            params += [f"%{q}%", f"%{q}%"]
        if role:
            where.append("role = ?")
            params.append(role)
        if status:
            where.append("status = ?")
            params.append(status)
        clause = " AND ".join(where)
        total = db.query_one(f"SELECT COUNT(*) AS n FROM users WHERE {clause}", tuple(params))
        rows = db.query_all(
            f"SELECT * FROM users WHERE {clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        return Page[UserOut](
            items=[_user_out(dict(r)) for r in rows],
            total=int(total["n"]) if total else 0,
            limit=limit,
            offset=offset,
        )

    return await db.arun(_run)


@router.post("/users", response_model=PasswordIssuedOut, status_code=201, summary="创建用户")
async def create_user(
    body: UserCreateIn,
    request: Request,
    admin: dict[str, Any] = Depends(require_admin),
) -> PasswordIssuedOut:
    """建号。未提供密码则生成随机强密码，**仅此一次**返回明文。"""
    password = body.password or auth_service.generate_password(16)

    def _run() -> dict[str, Any]:
        try:
            return auth_service.create_user(
                username=body.username,
                password=password,
                role=body.role,
                display_name=body.display_name,
                quota=body.quota,
                must_change_password=True,
                created_by=admin["id"],
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    row = await db.arun(_run)
    await db.arun(
        auth_service.audit, "user_create",
        actor_id=admin["id"], actor_name=admin.get("username"),
        target_type="user", target_id=row["id"],
        detail={"username": row["username"], "role": row["role"]}, ip=client_ip(request),
    )
    return PasswordIssuedOut(user=_user_out(row, with_usage=False), password=password)


@router.get("/users/{user_id}", response_model=UserOut, summary="用户详情")
async def get_user(user_id: str) -> UserOut:
    row = await db.arun(auth_service.get_user_row, user_id)
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")
    return await db.arun(_user_out, row)


@router.patch("/users/{user_id}", response_model=UserOut, summary="修改用户")
async def update_user(
    user_id: str,
    body: UserUpdateIn,
    request: Request,
    admin: dict[str, Any] = Depends(require_admin),
) -> UserOut:
    """改显示名/角色/状态/配额。用户名不可改（避免审计断链）。"""
    def _run() -> dict[str, Any]:
        row = auth_service.get_user_row(user_id)
        if not row:
            raise HTTPException(status_code=404, detail="用户不存在")

        # 防止把自己降级或停用，导致无人可管理
        if user_id == admin["id"]:
            if body.role and body.role != "admin":
                raise HTTPException(status_code=400, detail="不能修改自己的管理员角色")
            if body.status and body.status != "active":
                raise HTTPException(status_code=400, detail="不能停用自己的账号")
        # 防止移除最后一个管理员
        demoting = (body.role and body.role != "admin") or (body.status and body.status != "active")
        if row["role"] == "admin" and demoting and _admin_count_sync() <= 1:
            raise HTTPException(status_code=400, detail="系统至少需要保留一个启用状态的管理员")

        sets, params = [], []
        if body.display_name is not None:
            sets.append("display_name=?")
            params.append(body.display_name)
        if body.role is not None:
            sets.append("role=?")
            params.append(body.role)
        if body.status is not None:
            sets.append("status=?")
            params.append(body.status)
        if body.quota is not None:
            sets.append("quota_json=?")
            params.append(body.quota.model_dump_json())
        if not sets:
            raise HTTPException(status_code=422, detail="没有需要修改的字段")

        sets.append("updated_at=?")
        params.append(auth_service._fmt(auth_service._now()))
        params.append(user_id)
        db.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?", tuple(params))

        # 停用账号立即吊销其全部会话
        if body.status == "disabled":
            auth_service.destroy_user_sessions(user_id)

        updated = auth_service.get_user_row(user_id)
        assert updated is not None
        return updated

    row = await db.arun(_run)
    await db.arun(
        auth_service.audit, "user_update",
        actor_id=admin["id"], actor_name=admin.get("username"),
        target_type="user", target_id=user_id,
        detail=body.model_dump(exclude_none=True, mode="json"), ip=client_ip(request),
    )
    return await db.arun(_user_out, row)


@router.post(
    "/users/{user_id}/reset-password",
    response_model=PasswordIssuedOut,
    summary="重置密码",
)
async def reset_password(
    user_id: str,
    request: Request,
    admin: dict[str, Any] = Depends(require_admin),
) -> PasswordIssuedOut:
    """重置为随机强密码并强制用户下次登录改密；同时吊销其全部会话。"""
    password = auth_service.generate_password(16)

    def _run() -> dict[str, Any]:
        row = auth_service.get_user_row(user_id)
        if not row:
            raise HTTPException(status_code=404, detail="用户不存在")
        auth_service.set_password(user_id, password, must_change=True)
        auth_service.destroy_user_sessions(user_id)
        updated = auth_service.get_user_row(user_id)
        assert updated is not None
        return updated

    row = await db.arun(_run)
    await db.arun(
        auth_service.audit, "password_reset",
        actor_id=admin["id"], actor_name=admin.get("username"),
        target_type="user", target_id=user_id,
        detail={"username": row["username"]}, ip=client_ip(request),
    )
    return PasswordIssuedOut(user=_user_out(row, with_usage=False), password=password)


@router.post("/users/{user_id}/unlock", response_model=UserOut, summary="解除账号锁定")
async def unlock_user(
    user_id: str,
    request: Request,
    admin: dict[str, Any] = Depends(require_admin),
) -> UserOut:
    """解除锁定并清零失败计数。

    没有这条路径时，被锁的账号只能等锁定自然到期——而失败计数不复位意味着
    「到期」可以被攻击者无限推迟。管理员必须能在不登录受害者账号的前提下解锁。
    """
    def _run() -> dict[str, Any]:
        row = auth_service.get_user_row(user_id)
        if not row:
            raise HTTPException(status_code=404, detail="用户不存在")
        auth_service.unlock_user(user_id)
        auth_service.audit(
            "user_unlocked",
            actor_id=admin["id"], actor_name=admin["username"],
            target_type="user", target_id=user_id,
            detail={"username": row["username"]},
            ip=client_ip(request),
        )
        updated = auth_service.get_user_row(user_id)
        assert updated is not None
        return updated

    row = await db.arun(_run)
    return _user_out(row)


@router.delete("/users/{user_id}", response_model=OkMessage, summary="删除用户")
async def delete_user(
    user_id: str,
    request: Request,
    admin: dict[str, Any] = Depends(require_admin),
    purge_files: bool = Query(
        default=False,
        description="同时物理删除其上传件与交付物目录（不可撤销，需显式开启）",
    ),
) -> OkMessage:
    """删除用户及其全部数据（案件经外键级联清理）。"""
    def _run() -> dict[str, Any]:
        row = auth_service.get_user_row(user_id)
        if not row:
            raise HTTPException(status_code=404, detail="用户不存在")
        if user_id == admin["id"]:
            raise HTTPException(status_code=400, detail="不能删除自己的账号")
        if row["role"] == "admin" and _admin_count_sync() <= 1:
            raise HTTPException(status_code=400, detail="系统至少需要保留一个启用状态的管理员")

        case_rows = db.query_all("SELECT id FROM cases WHERE user_id=?", (user_id,))
        case_ids = [r["id"] for r in case_rows]
        # 默认不删盘：删账号是常见操作，磁盘上的原始材料与交付物却是不可再生的。
        # 备份的媒体侧对已删文件只保留有限的历史窗口，一旦过窗就真没了；
        # 「把删除做得更彻底」和「删除不可恢复」叠在一起，扩大的是不可逆损失面。
        # 需要连盘一起清时显式传 purge_files=true。
        if purge_files:
            import shutil

            from ..config import get_config

            cfg = get_config()
            for cid in case_ids:
                for base in (cfg.uploads_dir, cfg.outputs_dir):
                    target = base / cid
                    if target.is_dir():
                        shutil.rmtree(target, ignore_errors=True)

        db.execute("DELETE FROM cases WHERE user_id=?", (user_id,))
        db.execute("DELETE FROM users WHERE id=?", (user_id,))
        return {"username": row["username"], "cases": len(case_ids), "purged_files": purge_files}

    info = await db.arun(_run)
    await db.arun(
        auth_service.audit, "user_delete",
        actor_id=admin["id"], actor_name=admin.get("username"),
        target_type="user", target_id=user_id, detail=info, ip=client_ip(request),
    )
    tail = "，磁盘文件已一并删除" if purge_files else "；磁盘上的上传件与交付物已保留"
    return OkMessage(
        message=f"已删除用户「{info['username']}」及其 {info['cases']} 个案件{tail}"
    )


@router.get("/users/{user_id}/cases", summary="某用户的案件列表")
async def list_user_cases(
    user_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    def _run() -> dict[str, Any]:
        if not auth_service.get_user_row(user_id):
            raise HTTPException(status_code=404, detail="用户不存在")
        total = db.query_one("SELECT COUNT(*) AS n FROM cases WHERE user_id=?", (user_id,))
        rows = db.query_all(
            "SELECT id, module, title, patent_type, status, created_at, updated_at"
            " FROM cases WHERE user_id=? ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (user_id, limit, offset),
        )
        return {
            "items": [dict(r) for r in rows],
            "total": int(total["n"]) if total else 0,
            "limit": limit,
            "offset": offset,
        }

    return await db.arun(_run)


# ---------------------------------------------------------------------------
# 审计与统计
# ---------------------------------------------------------------------------


@router.get("/audit", response_model=Page[AuditEntryOut], summary="审计日志")
async def list_audit(
    action: str | None = Query(default=None),
    actor_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> Page[AuditEntryOut]:
    def _run() -> Page[AuditEntryOut]:
        where, params = ["1=1"], []
        if action:
            where.append("action = ?")
            params.append(action)
        if actor_id:
            where.append("actor_id = ?")
            params.append(actor_id)
        clause = " AND ".join(where)
        total = db.query_one(f"SELECT COUNT(*) AS n FROM audit_log WHERE {clause}", tuple(params))
        rows = db.query_all(
            f"SELECT * FROM audit_log WHERE {clause} ORDER BY created_at DESC, id DESC"
            " LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        items = []
        for r in rows:
            data = dict(r)
            try:
                detail = json.loads(data.get("detail_json") or "{}")
            except json.JSONDecodeError:
                detail = {}
            items.append(AuditEntryOut(
                id=data["id"], actor_id=data.get("actor_id"), actor_name=data.get("actor_name"),
                action=data["action"], target_type=data.get("target_type"),
                target_id=data.get("target_id"), target_owner=data.get("target_owner"),
                detail=detail, ip=data.get("ip"), created_at=data["created_at"],
            ))
        return Page[AuditEntryOut](
            items=items, total=int(total["n"]) if total else 0, limit=limit, offset=offset,
        )

    return await db.arun(_run)


@router.get("/stats", response_model=AdminStatsOut, summary="平台总览")
async def stats() -> AdminStatsOut:
    def _run() -> AdminStatsOut:
        from ..config import get_config

        now = auth_service._now()
        users = db.query_one("SELECT COUNT(*) AS n FROM users")
        active = db.query_one("SELECT COUNT(*) AS n FROM users WHERE status='active'")
        cases = db.query_one("SELECT COUNT(*) AS n FROM cases")
        by_module = db.query_all("SELECT module, COUNT(*) AS n FROM cases GROUP BY module")
        artifacts = db.query_one("SELECT COUNT(*) AS n FROM artifacts")
        calls = db.query_one(
            "SELECT SUM(llm_calls) AS n FROM usage_counters WHERE period=?",
            (now.strftime("%Y-%m-%d"),),
        )
        tokens = db.query_one(
            "SELECT SUM(total_tokens) AS n FROM usage_counters WHERE period=?",
            (now.strftime("%Y-%m"),),
        )

        cfg = get_config()
        total_bytes = 0
        for base in (cfg.uploads_dir, cfg.outputs_dir):
            if base.is_dir():
                total_bytes += sum(f.stat().st_size for f in base.rglob("*") if f.is_file())

        return AdminStatsOut(
            user_count=int(users["n"]) if users else 0,
            active_user_count=int(active["n"]) if active else 0,
            case_count=int(cases["n"]) if cases else 0,
            case_count_by_module={r["module"]: int(r["n"]) for r in by_module},
            llm_calls_today=int(calls["n"] or 0) if calls else 0,
            tokens_this_month=int(tokens["n"] or 0) if tokens else 0,
            storage_mb=round(total_bytes / 1024 / 1024, 2),
            artifact_count=int(artifacts["n"]) if artifacts else 0,
        )

    return await db.arun(_run)
