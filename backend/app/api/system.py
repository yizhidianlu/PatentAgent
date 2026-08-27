"""系统 API：健康检查与环境探测。"""

from __future__ import annotations

import importlib.util
import platform
import shutil
import sys
from pathlib import Path

from fastapi import APIRouter

from .. import APP_NAME, __version__
from ..config import get_config
from ..db import database as db

router = APIRouter(prefix="/system", tags=["系统"])

# 常见安装路径（Windows）
WINWORD_PATH = Path(r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE")
CHROME_CANDIDATES = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
]
EDGE_CANDIDATES = [
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
]


def _first_existing(paths: list[Path]) -> str | None:
    for p in paths:
        if p.is_file():
            return str(p)
    return None


@router.get("/health", summary="健康检查")
async def health() -> dict:
    return {
        "ok": True,
        "name": APP_NAME,
        "version": __version__,
        "time": db.now_str(),
    }


@router.get("/env", summary="环境探测：Word/soffice/浏览器/sqlite-vec/playwright/磁盘余量")
async def env() -> dict:
    cfg = get_config()

    def probe() -> dict:
        soffice = shutil.which("soffice")
        chrome = _first_existing(CHROME_CANDIDATES) or shutil.which("chrome")
        edge = _first_existing(EDGE_CANDIDATES) or shutil.which("msedge")
        playwright_ok = importlib.util.find_spec("playwright") is not None
        try:
            usage = shutil.disk_usage(cfg.data_dir)
            disk = {
                "total_gb": round(usage.total / 1024**3, 1),
                "free_gb": round(usage.free / 1024**3, 1),
            }
        except OSError as exc:
            disk = {"error": str(exc)}
        return {
            "python": {
                "version": sys.version.split()[0],
                "executable": sys.executable,
                "platform": platform.platform(),
            },
            "word": {"available": WINWORD_PATH.is_file(), "path": str(WINWORD_PATH)},
            "soffice": {"available": soffice is not None, "path": soffice},
            "chrome": {"available": chrome is not None, "path": chrome},
            "edge": {"available": edge is not None, "path": edge},
            "sqlite_vec": db.sqlite_vec_status,
            "playwright": {"importable": playwright_ok},
            "data_dir": {"path": str(cfg.data_dir), "disk": disk},
            "frontend_dist": {"available": cfg.frontend_dist.is_dir()},
        }

    return await db.arun(probe)
