# -*- coding: utf-8 -*-
"""案件媒体 API：把落在案件目录里的图片按 URL 交给浏览器。

**为什么需要这个端点。**
交底书正文里的插图写的是磁盘路径（``![图1](C:/.../uploads/xx.png)``），论文转专利
的附图落在 ``outputs/{case_id}/p2p_work/``。这两处 Word / PDF 导出器都直接读盘，
所以导出件里图是全的；但浏览器读不了本机路径（``C:`` 会被当成未知协议整个丢掉），
网页端于是只剩图题没有图——「只有导出的 Word 和 PDF 能看到图」正是这个成因。

**边界。**
路径由前端提供，而前端的路径又来自文档正文，也就是说它可以被模型生成的内容影响。
所以这里按「白名单目录 + 真实路径包含判定」收口：解析后的绝对路径必须落在本案件
自己的 uploads / outputs 目录内，扩展名必须在图片白名单里。越界一律 404 而不是
403——403 等于确认「这个路径存在」，那本身就是一点不该给的信息。
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import unquote
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse

from ..config import get_config
from ..db import database as db
from ..services import paths as paths_service
from .deps import client_ip, current_user, resolve_case_sync

logger = logging.getLogger(__name__)

router = APIRouter(tags=["媒体"])

# 只放行图片。这个端点是给 <img> 用的，不是通用文件下载口——
# 放宽到任意扩展名就等于把案件目录整个开成了可枚举的静态站点。
_MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
}


def _case_roots(case_id: str) -> list[Path]:
    """本案件允许被读取的两个根目录。"""
    cfg = get_config()
    return [cfg.outputs_dir / case_id, cfg.uploads_dir / case_id]


def _within(path: Path, root: Path) -> bool:
    try:
        root_real = root.resolve()
    except OSError:
        return False
    return path == root_real or root_real in path.parents


def resolve_media_path(case_id: str, raw: str) -> Path | None:
    """把正文里的图片引用解析成本案件目录内的真实文件；越界或不存在返回 None。

    路径的写法有四种，都要认——少认一种，调用方就得去猜该传哪种形态：
    - 绝对路径（交底书插图、AI 补图）
    - 相对工作目录的文件名（附图脚本回写的 ``png_path`` 有时只有文件名）
    - 相对案件输出目录的路径（mermaid / 公式 PNG 的 ``assets_dir``）
    - 相对 ``DATA_DIR`` 的路径（``outputs/<case>/图1.png``，即入库形态）
    """
    text = (raw or "").strip()
    if not text:
        return None
    if text.lower().startswith("file:///"):
        text = text[8:]
    elif text.lower().startswith("file://"):
        text = text[7:]

    # 兜底认一次「多编码了一层」的路径。
    #
    # markdown 的链接目标按 CommonMark 要做 URL 规范化，`patent_图1.png` 到了渲染器
    # 手里已经是 `patent_%E5%9B%BE1.png`；调用方若再编码一次，我们收到的就是
    # 一个字面上带 `%E5%9B%BE` 的文件名，于是**文件名带汉字的图全部 404**。
    # 前端已经修好，这里再兜一层：任何调用方多编码一次都不至于让图消失。
    #
    # 不放松边界：解出来的路径照样要过下面的包含判定，`%2e%2e%2f` 这类照样被挡。
    candidates_text = [text]
    if "%" in text:
        try:
            decoded = unquote(text)
        except (UnicodeDecodeError, ValueError):
            decoded = ""
        if decoded and decoded != text:
            candidates_text.append(decoded)

    roots = _case_roots(case_id)
    tries: list[Path] = []
    for item_text in candidates_text:
        candidate = Path(item_text)
        if candidate.is_absolute():
            tries.append(candidate)
            # 从别的机器/别的目录恢复过来的库，正文里留的是源机器的绝对路径。
            # 交给 paths 按数据目录锚点重定位——包含判定在后面照做，边界不受影响。
            relocated = paths_service.resolve(item_text)
            if relocated is not None:
                tries.append(relocated)
        else:
            for root in roots:
                tries.append(root / candidate)
                tries.append(root / "p2p_work" / candidate)
            # 也认「相对 DATA_DIR」的形态：`outputs/<case>/图1.png` 正是
            # artifacts.stored_path 现在的存储形态，把它直接递过来是很自然的写法。
            # 放行它不放松边界——下面的包含判定照做，别的案件的路径照样进不来。
            tries.append(paths_service.data_dir() / candidate)

    for item in tries:
        try:
            # strict=True：符号链接一并解开后再做包含判定，
            # 否则一个指向案件目录外的链接就能绕过整套边界。
            real = item.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not real.is_file():
            continue
        if real.suffix.lower() not in _MEDIA_TYPES:
            continue
        if any(_within(real, root) for root in roots):
            return real
    return None


@router.get("/cases/{case_id}/media", summary="按案件内路径读取图片（供网页端 <img> 内联显示）")
async def case_media(
    case_id: str,
    request: Request,
    path: str = Query(description="正文中的图片路径（绝对或相对案件目录）"),
    user: dict[str, Any] = Depends(current_user),
) -> FileResponse:
    await db.arun(resolve_case_sync, case_id, user, ip=client_ip(request))
    real = await db.arun(resolve_media_path, case_id, path)
    if real is None:
        # 不区分「不存在」与「越界」：区分本身就是一条信息
        raise HTTPException(status_code=404, detail="找不到该图片，或该路径不属于本案件")
    return FileResponse(
        real,
        media_type=_MEDIA_TYPES[real.suffix.lower()],
        # 图片按内容寻址（路径含时间戳/图号），可以放心让浏览器缓存
        headers={"Cache-Control": "private, max-age=3600"},
    )
