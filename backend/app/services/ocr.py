"""扫描件 OCR（识别专利扉页文字）。

老专利的公开文本多是扫描件：PDF 里没有文字层，国知局公布公告的检索结果里也没有摘要，
唯一有文字的 Google Patents 详情页又时常拿不到（限流、出口 IP 被标记）。这里用系统自带的
OCR 引擎把扉页认出来——Windows 装了「中文(简体)」语言包即可用，不需要再装别的东西。

只做两件事：判断引擎是否可用、把一张图 / 一个 PDF 的扉页识别成文字。怎么从文字里取摘要
是 `patent_fetch.parse_front_page_text` 的事。**任何失败都不抛**，返回空串或 False。
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import tempfile
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "win_ocr.ps1"
LANG = "zh-Hans-CN"
# 一次识别的上限（扉页一页通常 1s 内；首次加载引擎慢一些）
OCR_TIMEOUT = 60
# 扫描件渲染倍率：72dpi 的 A4 放大 4 倍约 2400×3400。识别精度随分辨率明显上升
# （同一张扉页 1240px 宽时摘要相似度 0.90，2480px 时 0.98），且远低于引擎 10000px 上限。
RENDER_ZOOM = 4.0
# 站点给的扉页图只有 1240px 宽：先放大到这个宽度再识别
TARGET_WIDTH = 2400
# 引擎按词返回，中文会被空格切成一个个字：「纤 维 支 气 管 镜」。汉字之间的空白一律去掉。
_CJK_GAP_RE = re.compile(r"(?<=[一-鿿　-〿＀-￯])\s+|\s+(?=[一-鿿　-〿＀-￯])")


def _powershell(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT), *args]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(
        cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout, check=False,
        creationflags=flags,
    )


@lru_cache(maxsize=1)
def available() -> bool:
    """本机是否有中文 OCR 引擎（仅 Windows；结果按进程缓存）。"""
    if sys.platform != "win32" or not SCRIPT.exists():
        return False
    try:
        proc = _powershell(["-Probe"], timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.info("OCR 引擎探测失败：%s", exc)
        return False
    langs = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    ok = proc.returncode == 0 and any(lang.lower().startswith("zh-hans") for lang in langs)
    if not ok:
        logger.info("本机没有中文 OCR 引擎（可用语言：%s）", "、".join(langs) or "无")
    return ok


def unavailable_reason() -> str:
    if sys.platform != "win32":
        return "本机不是 Windows，没有系统自带的 OCR 引擎"
    return "本机没有中文 OCR 引擎（Windows 需安装「中文(简体)」语言包）"


def image_text(path: Path, *, timeout: int = OCR_TIMEOUT) -> str:
    """识别一张图片，返回按行拼接的文字（汉字间的空格已去掉）；失败返回空串。"""
    try:
        proc = _powershell(["-Path", str(path), "-Lang", LANG], timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("OCR 失败：%s", exc)
        return ""
    if proc.returncode != 0:
        logger.warning("OCR 退出码 %s：%s", proc.returncode, (proc.stderr or "").strip()[:300])
        return ""
    return normalize_lines(proc.stdout or "")


def normalize_lines(text: str) -> str:
    """去掉引擎在汉字之间插的空格，保留换行（著录项按行解析）。"""
    return "\n".join(_CJK_GAP_RE.sub("", line).strip() for line in text.splitlines() if line.strip())


def image_text_upscaled(path: Path, *, target_width: int = TARGET_WIDTH, timeout: int = OCR_TIMEOUT) -> str:
    """窄图先放大再识别（站点给的扉页图只有 1240px 宽，直接识别错字明显多）。放不大就按原图识别。"""
    import fitz  # PyMuPDF；延迟导入

    tmp = Path(tempfile.gettempdir()) / f"patent-ocr-{os.getpid()}-{id(path)}-up.png"
    try:
        with fitz.open(str(path)) as doc:
            page = doc[0]
            width = fitz.Pixmap(str(path)).width
            if width >= target_width:
                return image_text(path, timeout=timeout)
            zoom = (width / page.rect.width) * (target_width / width)   # 图片文档 1pt = 1px/72dpi
            page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY).save(str(tmp))
        return image_text(tmp, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 —— 放大失败就按原图来
        logger.info("扉页图放大失败，按原图识别：%s", exc)
        return image_text(path, timeout=timeout)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def pdf_front_page_text(pdf: bytes, *, page_index: int = 0, timeout: int = OCR_TIMEOUT) -> str:
    """把 PDF 的一页渲成灰度图交给 OCR；失败返回空串。"""
    import fitz  # PyMuPDF；延迟导入

    tmp = Path(tempfile.gettempdir()) / f"patent-ocr-{os.getpid()}-{id(pdf)}.png"
    try:
        with fitz.open(stream=pdf, filetype="pdf") as doc:
            if page_index >= len(doc):
                return ""
            pix = doc[page_index].get_pixmap(matrix=fitz.Matrix(RENDER_ZOOM, RENDER_ZOOM), colorspace=fitz.csGRAY)
            pix.save(str(tmp))
        return image_text(tmp, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 —— OCR 是兜底，失败只是取不到摘要
        logger.warning("扫描件渲染 / 识别失败：%s", exc)
        return ""
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
