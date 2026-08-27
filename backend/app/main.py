"""引途医疗专利智能体 — FastAPI 应用入口。

- CORS：来源白名单读 `AppConfig.allowed_origins`（生产收紧为部署域名）；
- 中间件链（外 → 内）：安全响应头 → CORS → 认证守卫/CSRF → 路由；
- startup：建数据目录 + 跑数据库迁移 + 首启建管理员；
- /api/v1 挂载各业务 router（含 /auth 与 /admin）；
- 若 frontend/dist 存在则静态挂载并提供 SPA fallback（**/api 前缀完全不匹配**）。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import anyio.to_thread
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Match, Mount
from starlette.staticfiles import StaticFiles

from . import APP_NAME, __version__
from .api import admin as admin_api
from .api import artifacts as artifacts_api
from .api import auth as auth_api
from .api import cases as cases_api
from .api import disclosure as disclosure_api
from .api import events as events_api
from .api import files as files_api
from .api import media as media_api
from .api import oa as oa_api
from .api import p2p as p2p_api
from .api import pipeline as pipeline_api
from .api import reader as reader_api
from .api import render as render_api
from .api import search as search_api
from .api import settings as settings_api
from .api import skills as skills_api
from .api import system as system_api
from .config import get_config, unknown_env_keys
from .db import database as db
from .middleware import AuthMiddleware, SecurityHeadersMiddleware
from .pipelines import engine as pipeline_engine
from .services import auth as auth_service
from .services.instance_lock import InstanceLock

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动：日志、数据目录、数据库迁移；关停：关闭连接。"""
    cfg = get_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # .env 里拼错的键会被 pydantic 静默忽略——生产上最典型的后果是
    # COOKIE_SECURE 写错，Cookie 不带 Secure 却毫无提示。启动时报出来。
    if (unknown := unknown_env_keys()):
        logger.warning(
            "backend/.env 中有 %d 个无法识别的配置项，已被忽略（请核对拼写）：%s",
            len(unknown), "、".join(unknown),
        )
    cfg.ensure_dirs()
    await anyio.to_thread.run_sync(db.init_db)
    # 首次启动引导：库中无用户时创建管理员（随机密码在日志里醒目打印一次）
    await anyio.to_thread.run_sync(auth_service.ensure_bootstrap_admin)
    # 启动恢复钩子：上次进程死亡遗留的 running 状态 → failed('interrupted')
    #
    # 只有拿到单实例锁才能做——这一步是破坏性的、且不区分「这条 run 属于谁」。
    # uvicorn 先跑完 lifespan 再绑端口，所以一个注定撞 address-in-use 的多余实例
    # 也会完整走到这里：它会把另一个进程正在跑的流水线判死，然后自己退出，
    # 留下一条被判死却仍在被写入的记录。几十分钟的产出就此报废，
    # 而 failed 不是 waiting_user，走不了 resume。
    instance_lock = InstanceLock(cfg.data_dir)
    app.state.instance_lock = instance_lock
    if instance_lock.acquire():
        recovered = await anyio.to_thread.run_sync(pipeline_engine.recover_interrupted)
        if recovered["runs_failed"] or recovered["cases_failed"]:
            logger.info(
                "流水线启动恢复：runs→failed %s 条，cases→failed %s 条（可经 /pipeline/resume 续跑）",
                recovered["runs_failed"], recovered["cases_failed"],
            )
    else:
        logger.warning(
            "检测到同一数据目录上已有实例在运行，跳过启动恢复钩子。"
            "本进程多半是重复启动，稍后会因端口占用退出——"
            "跳过是对的：否则会把另一个进程正在跑的流水线判成中断。"
        )

    logger.info("%s 启动完成（数据目录：%s）", APP_NAME, cfg.data_dir)
    yield
    instance_lock.release()
    db.close_db()


def _is_api_path(path: str) -> bool:
    """是否属于 API 命名空间（/api 与 /api/... 都算）。"""
    return path == "/api" or path.startswith("/api/")


class SpaStaticFiles(StaticFiles):
    """静态文件服务 + SPA fallback：未命中的非 /api 路径回退到 index.html。"""

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and not _is_api_path(scope["path"]):
                return await super().get_response("index.html", scope)
            raise


class SpaMount(Mount):
    """SPA 静态挂载点：对 `/api` 前缀**一律不匹配**。

    Starlette 的 `Mount('/')` 对任何路径都是 FULL 匹配，会盖过 API 路由的
    「路径命中但方法不匹配」PARTIAL 匹配 —— 于是 `GET /api/v1/render/mermaid`
    （只支持 POST）会掉进静态文件里返回 404 而不是 405（3 号 tester 报告的 P2）。
    这里让挂载点直接放弃 /api 命名空间，把状态码的决定权还给 API 路由，
    同时确保 /api 的响应永远是 JSON、永远不会是 index.html。
    """

    def matches(self, scope):  # noqa: ANN001, ANN201
        if _is_api_path(scope.get("path") or ""):
            return Match.NONE, {}
        return super().matches(scope)


def create_app() -> FastAPI:
    """构造 FastAPI 实例（供 uvicorn 与测试共用）。"""
    cfg = get_config()
    app = FastAPI(
        title=APP_NAME,
        version=__version__,
        description="引途医疗专利智能体后端 API（多用户服务端部署）",
        lifespan=lifespan,
    )

    # 中间件：**后添加者在更外层**。期望链路（外 → 内）：
    #   安全响应头 → CORS → 认证守卫/CSRF → 路由
    # CORS 必须在认证之外，否则 401/403 响应缺 CORS 头，浏览器读不到错误详情；
    # 安全头在最外层，才能覆盖到中间件直接返回的 401/403。
    app.add_middleware(AuthMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(cfg.allowed_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(SecurityHeadersMiddleware)

    # /api/v1 业务路由
    api_prefix = "/api/v1"
    app.include_router(system_api.router, prefix=api_prefix)
    app.include_router(auth_api.router, prefix=api_prefix)
    app.include_router(admin_api.router, prefix=api_prefix)
    app.include_router(settings_api.router, prefix=api_prefix)
    app.include_router(skills_api.router, prefix=api_prefix)
    app.include_router(cases_api.router, prefix=api_prefix)
    app.include_router(events_api.router, prefix=api_prefix)
    app.include_router(files_api.router, prefix=api_prefix)
    app.include_router(media_api.router, prefix=api_prefix)
    app.include_router(pipeline_api.router, prefix=api_prefix)
    app.include_router(p2p_api.router, prefix=api_prefix)
    app.include_router(disclosure_api.router, prefix=api_prefix)
    app.include_router(reader_api.router, prefix=api_prefix)
    app.include_router(search_api.router, prefix=api_prefix)
    app.include_router(render_api.router, prefix=api_prefix)
    app.include_router(artifacts_api.router, prefix=api_prefix)
    app.include_router(oa_api.router, prefix=api_prefix)

    # 前端构建产物存在则静态挂载（SPA fallback，/api 前缀完全不参与匹配）
    dist = cfg.frontend_dist
    if dist.is_dir():
        app.router.routes.append(
            SpaMount("/", app=SpaStaticFiles(directory=str(dist), html=True), name="spa")
        )
        logger.info("已挂载前端静态资源：%s", dist)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=get_config().port)
