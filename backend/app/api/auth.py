"""认证 API：登录 / 登出 / 当前用户 / 修改密码（M8）。

对应 docs/design/auth-system.md §5.1。会话走 httpOnly Cookie，
CSRF token 另发一个非 httpOnly Cookie 供前端读取并回填请求头。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from ..config import get_config
from ..db import database as db
from ..models.auth import (
    ChangePasswordIn,
    LoginIn,
    LoginOut,
    RegisterIn,
    RegisterOut,
    RegistrationStateOut,
    UserOut,
)
from ..models.common import Ok, OkMessage
from ..services import auth as auth_service
from ..services.rate_limit import registration_limiter
from .deps import client_ip, current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["认证"])


def _cookie_secure() -> bool:
    """生产必须置 true（仅经 HTTPS 传输 Cookie）。"""
    return bool(getattr(get_config(), "cookie_secure", False))


def _set_session_cookies(response: Response, session_id: str, csrf_token: str) -> None:
    max_age = auth_service.SESSION_IDLE_DAYS * 24 * 3600
    response.set_cookie(
        auth_service.SESSION_COOKIE, session_id,
        max_age=max_age, httponly=True, samesite="lax",
        secure=_cookie_secure(), path="/",
    )
    # CSRF token 需要被前端 JS 读取后回填到 X-CSRF-Token，故不能 httpOnly
    response.set_cookie(
        auth_service.CSRF_COOKIE, csrf_token,
        max_age=max_age, httponly=False, samesite="lax",
        secure=_cookie_secure(), path="/",
    )


def _clear_session_cookies(response: Response) -> None:
    response.delete_cookie(auth_service.SESSION_COOKIE, path="/")
    response.delete_cookie(auth_service.CSRF_COOKIE, path="/")


def _user_out(row: dict[str, Any], *, with_usage: bool = False) -> UserOut:
    usage = auth_service.usage_snapshot(row["id"]) if with_usage else None
    return auth_service._row_to_user(row, usage=usage)


@router.post("/login", response_model=LoginOut, summary="登录")
async def login(body: LoginIn, request: Request, response: Response) -> Any:
    """校验凭据并签发会话。

    失败一律返回 401；账号被锁定/停用时给出明确原因（此时告知原因的
    安全收益大于泄露风险——用户需要知道该去找管理员）。
    """
    ip = client_ip(request)
    try:
        row = await db.arun(auth_service.authenticate, body.username, body.password)
    except auth_service.AuthError as exc:
        await db.arun(
            auth_service.audit, "login_failed",
            actor_name=body.username, detail={"code": exc.code, "reason": str(exc)}, ip=ip,
        )
        # 扁平的结构化错误体：`detail` 仍是可直接展示的中文（与其它端点一致、
        # 也向后兼容），`code` 与 `retry_after` 平级附加，前端据此决定提示形态
        # 与锁定倒计时，不必从文案里正则抠数字。
        # 这里不用 HTTPException(detail=dict)——那会产出 {"detail":{"detail":...}}
        # 的嵌套，前端取值层级别扭。
        payload: dict[str, Any] = {"detail": str(exc), "code": exc.code}
        if exc.retry_after:
            payload["retry_after"] = exc.retry_after
        return JSONResponse(payload, status_code=401)

    session = await db.arun(
        auth_service.create_session, row["id"],
        ip=ip, user_agent=request.headers.get("user-agent"),
    )
    _set_session_cookies(response, session["session_id"], session["csrf_token"])
    await db.arun(
        auth_service.audit, "login",
        actor_id=row["id"], actor_name=row["username"], ip=ip,
    )
    return LoginOut(user=_user_out(row, with_usage=True), csrf_token=session["csrf_token"])


@router.get("/registration-open", response_model=RegistrationStateOut, summary="是否开放自助注册")
async def registration_state() -> RegistrationStateOut:
    """登录页据此决定要不要显示「注册」入口。公开接口，不含任何账号信息。"""
    return RegistrationStateOut(open=await db.arun(auth_service.registration_open))


@router.post("/register", response_model=RegisterOut, status_code=201, summary="自助注册")
async def register(body: RegisterIn, request: Request) -> Any:
    """自助注册一个待审核账号。

    有意**不签发会话**：注册这一步不该带来任何权限，账号要等管理员放行。
    注册接口必须如实告知「用户名被占用」——那是用户改名的唯一依据，
    藏也藏不住（换个名字再试一次就知道了），所以这里不做登录那套模糊化处理。
    """
    ip = client_ip(request)

    # 限流必须在 register_user 之前——它内部会做 argon2（64 MiB / 约 50ms），
    # 而那正是被放大的那一步。放到之后等于没限。
    allowed, retry_after = registration_limiter.check(ip or "unknown")
    if not allowed:
        return JSONResponse(
            {
                "detail": f"注册过于频繁，请 {retry_after} 秒后再试。",
                "code": "rate_limited",
                "retry_after": retry_after,
            },
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )

    try:
        row = await db.arun(
            auth_service.register_user, body.username, body.password, body.display_name,
        )
    except auth_service.AuthError as exc:
        # 只有「注册开着、但这次被拒」才值得留档。注册关闭时的拒绝不写审计——
        # 否则关掉注册也挡不住匿名灌库：攻击者继续 POST，audit_log 就一直长，
        # 而它与客户的专利原件同盘；更糟的是真正的 login / cross_user_read 记录
        # 会被冲到几万行之后，等于把审计日志本身废掉。
        # 限流已经挡在前面，能走到这里的量是有界的。
        if exc.code != "registration_closed":
            await db.arun(
                auth_service.audit, "register_rejected",
                actor_name=body.username, detail={"code": exc.code, "reason": str(exc)}, ip=ip,
            )
        return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=400)
    except ValueError as exc:   # create_user 的用户名冲突兜底
        return JSONResponse({"detail": str(exc), "code": "username_taken"}, status_code=400)

    await db.arun(
        auth_service.audit, "register",
        actor_id=row["id"], actor_name=row["username"], ip=ip,
    )
    return RegisterOut(
        status="pending",
        message="注册成功。账号需管理员审核通过后才能登录，请稍后再试或联系管理员。",
    )


@router.post("/logout", response_model=Ok, summary="登出")
async def logout(request: Request, response: Response) -> Ok:
    """销毁当前会话（未登录也返回成功，保持幂等）。"""
    session_id = request.cookies.get(auth_service.SESSION_COOKIE, "")
    if session_id:
        session = await db.arun(auth_service.load_session, session_id)
        if session:
            await db.arun(
                auth_service.audit, "logout",
                actor_id=session["user_id"], ip=client_ip(request),
            )
        await db.arun(auth_service.destroy_session, session_id)
    _clear_session_cookies(response)
    return Ok()


@router.get("/me", response_model=UserOut, summary="当前登录用户")
async def me(user: dict[str, Any] = Depends(current_user)) -> UserOut:
    return await db.arun(_user_out, user, with_usage=True)


@router.post("/change-password", response_model=OkMessage, summary="修改密码")
async def change_password(
    body: ChangePasswordIn,
    request: Request,
    response: Response,
    user: dict[str, Any] = Depends(current_user),
) -> OkMessage:
    """改密后吊销该用户的其它全部会话（当前会话保留）。"""
    if not await db.arun(auth_service.verify_password, user["password_hash"], body.old_password):
        raise HTTPException(status_code=400, detail="当前密码不正确")
    if body.old_password == body.new_password:
        raise HTTPException(status_code=400, detail="新密码不能与当前密码相同")

    await db.arun(auth_service.set_password, user["id"], body.new_password, must_change=False)
    current_session = request.cookies.get(auth_service.SESSION_COOKIE, "")
    await db.arun(auth_service.destroy_user_sessions, user["id"], keep=current_session)
    await db.arun(
        auth_service.audit, "password_change",
        actor_id=user["id"], actor_name=user.get("username"), ip=client_ip(request),
    )
    return OkMessage(message="密码已修改，其它设备上的登录已退出")
