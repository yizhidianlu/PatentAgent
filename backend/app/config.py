"""应用配置（pydantic-settings）。

约定：
- DATA_DIR 默认为项目根目录下的 data/（可用环境变量 DATA_DIR 覆盖，亦支持 backend/.env）；
- PORT 默认 8000；
- LOG_LEVEL 默认 INFO。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录：backend/app/config.py → 上溯两级到 PatentAgent/
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]


class AppConfig(BaseSettings):
    """全局运行配置（进程级；与 DB 内的用户设置 settings 表相互独立）。"""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / "backend" / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # 运行时数据目录（SQLite 库、上传件、交付物均落在其下）
    data_dir: Path = PROJECT_ROOT / "data"
    # 服务监听端口
    port: int = 8000
    # 日志级别
    log_level: str = "INFO"

    # ---- 账号系统与部署安全（M8）----
    # 会话 Cookie 仅经 HTTPS 传输；**生产部署必须置 true**（置于反向代理之后时同样需要）
    cookie_secure: bool = False
    # 允许的前端来源（CORS）；生产应收紧为实际部署域名
    allowed_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    # 首次启动创建管理员用的凭据（不设则随机生成密码并在日志打印一次）
    admin_username: str = "admin"
    admin_password: str = ""

    # ---- 派生路径 ----

    @property
    def db_path(self) -> Path:
        """SQLite 主库路径。"""
        return self.data_dir / "app.db"

    @property
    def uploads_dir(self) -> Path:
        """原始上传件目录（按案件分目录）。"""
        return self.data_dir / "uploads"

    @property
    def outputs_dir(self) -> Path:
        """版本化交付物目录（按案件分目录，时间戳命名，永不覆盖）。"""
        return self.data_dir / "outputs"

    @property
    def tmp_dir(self) -> Path:
        """临时文件目录。"""
        return self.data_dir / "tmp"

    @property
    def frontend_dist(self) -> Path:
        """前端构建产物目录（存在则静态挂载）。"""
        return PROJECT_ROOT / "frontend" / "dist"

    def ensure_dirs(self) -> None:
        """确保运行时目录存在。"""
        for p in (self.data_dir, self.uploads_dir, self.outputs_dir, self.tmp_dir):
            p.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """取全局配置单例（测试可先设环境变量再 cache_clear()）。"""
    return AppConfig()
