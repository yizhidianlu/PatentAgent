"""认证与授权依赖（M8）。

对应 docs/design/auth-system.md §4「三层防线」的第 2、3 层：

    第 2 层 —— 依赖注入：current_user() / require_admin()
    第 3 层 —— 数据访问：resolve_case() 统一做属主校验

**实施纪律**：业务代码禁止直接 `SELECT * FROM cases WHERE id=?`，
一律走 resolve_case()。普通用户命中他人案件返回 404（不是 403），
避免通过状态码探测他人 case_id 是否存在。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Depends, HTTPException, Request

from ..db import database as db
from ..services import auth as auth_service

logger = logging.getLogger(__name__)


def client_ip(request: Request) -> str | None:
    """取客户端 IP（信任反向代理的 X-Forwarded-For 首段）。"""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


async def current_user(request: Request) -> dict[str, Any]:
    """当前登录用户；未登录/会话失效/账号被停用 → 401。

    中间件已拦截未认证请求，这里再取一次是为了：
    (1) 让路由拿到用户行；(2) 即便将来中间件白名单配错也不至于裸奔。
    """
    user = getattr(request.state, "user", None)
    if user:
        return user

    session_id = request.cookies.get(auth_service.SESSION_COOKIE, "")
    session = auth_service.load_session(session_id) if session_id else None
    if not session:
        raise HTTPException(status_code=401, detail="尚未登录或登录已过期，请重新登录")

    row = auth_service.get_user_row(session["user_id"])
    if not row:
        auth_service.destroy_session(session_id)
        raise HTTPException(status_code=401, detail="账号不存在，请重新登录")
    if (row.get("status") or "active") != "active":
        # 管理员停用账号后，其已签发的会话必须立即失效
        auth_service.destroy_user_sessions(row["id"])
        raise HTTPException(status_code=401, detail="账号已被停用，请联系管理员")

    request.state.user = row
    request.state.session = session
    return row


async def require_admin(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    """要求管理员角色。"""
    if (user.get("role") or "user") != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def is_admin(user: dict[str, Any] | None) -> bool:
    return bool(user) and (user.get("role") or "user") == "admin"


# ---------------------------------------------------------------------------
# 数据隔离
# ---------------------------------------------------------------------------


def resolve_case_sync(
    case_id: str,
    user: dict[str, Any],
    *,
    ip: str | None = None,
    write: bool = False,
) -> dict[str, Any]:
    """取案件并校验属主。

    - 属主本人：直接放行；
    - 管理员访问他人案件：只读放行 + 写审计日志；`write=True` 时拒绝
      （管理员可查看与删除，但不得篡改他人案件内容）；
    - 其他情况：404（不泄露案件是否存在）。
    """
    row = db.query_one("SELECT * FROM cases WHERE id=?", (case_id,))
    if not row:
        raise HTTPException(status_code=404, detail="案件不存在")
    case = dict(row)
    owner = case.get("user_id") or ""

    if owner and owner == user["id"]:
        return case

    if is_admin(user):
        if write:
            raise HTTPException(
                status_code=403,
                detail="管理员可以查看他人案件，但不能修改其内容",
            )
        auth_service.audit(
            "cross_user_read",
            actor_id=user["id"],
            actor_name=user.get("username"),
            target_type="case",
            target_id=case_id,
            target_owner=owner,
            detail={"module": case.get("module"), "title": case.get("title")},
            ip=ip,
        )
        case["_cross_user"] = True
        return case

    raise HTTPException(status_code=404, detail="案件不存在")


async def resolve_case(
    case_id: str,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    """resolve_case_sync 的异步包装（供 FastAPI 依赖使用）。"""
    return await db.arun(resolve_case_sync, case_id, user, ip=client_ip(request))


def _reraise_as(exc: HTTPException, detail: str) -> HTTPException:
    """把 resolve_case_sync 的 404 换成资源自身的措辞（仍是 404，不泄露归属）。"""
    if exc.status_code == 404:
        return HTTPException(status_code=404, detail=detail)
    return exc


def resolve_file_sync(
    file_id: str,
    user: dict[str, Any],
    *,
    ip: str | None = None,
    write: bool = False,
) -> dict[str, Any]:
    """取上传件并经其所属案件做属主校验；非本人（非管理员）一律 404。"""
    row = db.query_one("SELECT * FROM files WHERE id=?", (file_id,))
    if not row:
        raise HTTPException(status_code=404, detail="文件不存在")
    data = dict(row)
    try:
        resolve_case_sync(str(data.get("case_id") or ""), user, ip=ip, write=write)
    except HTTPException as exc:
        raise _reraise_as(exc, "文件不存在") from exc
    return data


def resolve_artifact_sync(
    artifact_id: str,
    user: dict[str, Any],
    *,
    ip: str | None = None,
    write: bool = False,
) -> dict[str, Any]:
    """取交付物并经其所属案件做属主校验；非本人（非管理员）一律 404。"""
    row = db.query_one("SELECT * FROM artifacts WHERE id=?", (artifact_id,))
    if not row:
        raise HTTPException(status_code=404, detail="交付物不存在")
    data = dict(row)
    try:
        resolve_case_sync(str(data.get("case_id") or ""), user, ip=ip, write=write)
    except HTTPException as exc:
        raise _reraise_as(exc, "交付物不存在") from exc
    return data


def enforce_quota(user: dict[str, Any]) -> None:
    """发起会烧 LLM 配额的长流程前校验用量；超限 → 429。"""
    try:
        auth_service.check_quota(user)
    except auth_service.QuotaExceededError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc


def viewer_of(user: dict[str, Any]) -> dict[str, Any]:
    """精简的「可见性主体」（供 services 层做属主过滤，不传整行用户）。"""
    return {"id": user.get("id") or "", "role": user.get("role") or "user"}


def owned_case_ids_clause(user: dict[str, Any]) -> tuple[str, tuple]:
    """构造列表查询用的属主过滤片段。

    管理员不加过滤（可见全部）；普通用户限定自己的案件。
    返回 (SQL 片段, 参数元组)，调用方拼进 WHERE。
    """
    if is_admin(user):
        return "", ()
    return " AND cases.user_id = ? ", (user["id"],)
