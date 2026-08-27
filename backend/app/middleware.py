"""全局中间件：认证守卫、CSRF 校验、安全响应头（M8）。

对应 docs/design/auth-system.md §4「三层防线」第 1 层与 §8 部署加固。

设计要点：
- **白名单极简**：只放行登录、健康检查与静态资源，其余一律要求已登录；
- 未认证的 /api 请求返回 401 JSON（前端据此跳登录页），非 /api 请求交给
  SPA fallback（前端路由守卫会自己跳登录页）；
- CSRF 采用双提交模式：非幂等方法必须带 X-CSRF-Token 且与会话内的值一致。
"""

from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .services import auth as auth_service

logger = logging.getLogger(__name__)

API_PREFIX = "/api/"

# 无需登录即可访问的 API 路径（精确匹配）
PUBLIC_API_PATHS: frozenset[str] = frozenset({
    "/api/v1/auth/login",
    "/api/v1/system/health",
})

# 免 CSRF 校验的路径：登录本身（此时还没有会话）
CSRF_EXEMPT_PATHS: frozenset[str] = frozenset({
    "/api/v1/auth/login",
})

SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})


def _unauthorized(detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=401)


class AuthMiddleware(BaseHTTPMiddleware):
    """认证守卫 + CSRF 校验。

    认证结果挂到 `request.state.user` / `request.state.session`，
    下游依赖 `deps.current_user()` 直接复用，避免重复查库。
    """

    async def dispatch(self, request: Request, call_next):  # noqa: ANN001, ANN201
        path = request.url.path

        # 非 API 请求（SPA 页面与静态资源）不在此拦截——前端有自己的路由守卫，
        # 且真正的数据访问都要走 API，拦在 API 层就够。
        if not path.startswith(API_PREFIX):
            return await call_next(request)

        if path in PUBLIC_API_PATHS or request.method == "OPTIONS":
            return await call_next(request)

        session_id = request.cookies.get(auth_service.SESSION_COOKIE, "")
        if not session_id:
            return _unauthorized("尚未登录，请先登录")

        session = auth_service.load_session(session_id)
        if not session:
            return _unauthorized("登录已过期，请重新登录")

        user = auth_service.get_user_row(session["user_id"])
        if not user:
            auth_service.destroy_session(session_id)
            return _unauthorized("账号不存在，请重新登录")
        if (user.get("status") or "active") != "active":
            # 管理员停用账号后，已签发会话立即作废
            auth_service.destroy_user_sessions(user["id"])
            return _unauthorized("账号已被停用，请联系管理员")

        # CSRF：非幂等方法必须带正确的 token
        if request.method not in SAFE_METHODS and path not in CSRF_EXEMPT_PATHS:
            supplied = request.headers.get(auth_service.CSRF_HEADER, "")
            if not supplied or supplied != session.get("csrf_token"):
                return JSONResponse(
                    {"detail": "CSRF 校验失败，请刷新页面后重试"}, status_code=403
                )

        # 首登强制改密：除改密与登出外，其余接口一律拦下
        if user.get("must_change_password") and path not in {
            "/api/v1/auth/change-password",
            "/api/v1/auth/logout",
            "/api/v1/auth/me",
        }:
            return JSONResponse(
                {"detail": "首次登录需要先修改密码", "code": "must_change_password"},
                status_code=403,
            )

        request.state.user = user
        request.state.session = session
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """安全响应头（§8）。

    CSP 允许 'unsafe-inline'：前端的无闪烁主题脚本是内联的，且 mermaid 渲染
    需要内联样式；这在本产品的威胁模型下可接受（无第三方内容注入面）。
    """

    async def dispatch(self, request: Request, call_next):  # noqa: ANN001, ANN201
        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "font-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'",
        )
        return response
