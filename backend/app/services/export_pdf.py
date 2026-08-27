"""docx → pdf 导出（M2）。

三级链（backend-architecture.md §0 决策 4 / §6 风险 2）：
① Word COM（主路径，Word 已装）：
   - 专用线程内 pythoncom.CoInitialize()/CoUninitialize()；
   - 模块级 asyncio.Lock 串行（Word COM 不可并发）；
   - DisplayAlerts 关闭、Visible=False；直接 win32com 调 Word（docx2pdf 包内部
     即此路径，但其不暴露 DisplayAlerts 开关，故此处直控 COM）；
② 探测到 soffice（LibreOffice）则子进程 --headless --convert-to pdf；
③ 全部失败：抛 PdfExportError，错误信息汇总各引擎失败原因
   （Pillow 图片版兜底仅论文转专利模块可用，M3 实现）。

probe() 报告当前可用引擎，供 /system 与前端提示。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import shutil
import subprocess
import sys
from pathlib import Path

import anyio.to_thread
from ulid import ULID

from ..config import get_config
from ..db import database as db

logger = logging.getLogger(__name__)

# Word 常见安装路径（探测用；COM 调用不依赖该路径）
WINWORD_PATH = Path(r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE")

# Word SaveAs 的 PDF 格式常量（wdFormatPDF）
_WD_FORMAT_PDF = 17

# soffice 转换超时（秒）
SOFFICE_TIMEOUT = 300

# 模块级锁：Word COM 全局串行（asyncio.Lock 自 Py3.10 起创建时不绑定事件循环）
_word_lock = asyncio.Lock()


class PdfExportError(RuntimeError):
    """docx → pdf 全部引擎失败。"""


def _soffice_path() -> str | None:
    """探测 LibreOffice 的 soffice 可执行文件。"""
    return shutil.which("soffice")


def _word_available() -> bool:
    """Word COM 是否可用：pywin32 可导入 + 平台为 Windows（WINWORD 路径仅作参考）。"""
    if sys.platform != "win32":
        return False
    return importlib.util.find_spec("pythoncom") is not None


def probe() -> dict:
    """报告可用 PDF 引擎与优先顺序（settings.general.pdf_engine 可强制指定）。"""
    word_ok = _word_available()
    soffice = _soffice_path()
    order: list[str] = []
    if word_ok:
        order.append("word")
    if soffice:
        order.append("soffice")
    return {
        "word": {"available": word_ok, "winword_path": str(WINWORD_PATH), "winword_exists": WINWORD_PATH.is_file()},
        "soffice": {"available": soffice is not None, "path": soffice},
        "order": order,
    }


def _docx_to_pdf_word_sync(docx_path: Path, pdf_path: Path) -> None:
    """Word COM 转换（必须在专用线程内调用；调用方需已持 _word_lock）。"""
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()  # 专用线程 STA 初始化
    try:
        # DispatchEx：独立 Word 实例，避免干扰用户已开的 Word
        word = win32com.client.DispatchEx("Word.Application")
        try:
            word.Visible = False
            word.DisplayAlerts = 0  # wdAlertsNone：关闭一切弹窗，防止卡死
            doc = word.Documents.Open(str(docx_path), ReadOnly=True)
            try:
                doc.SaveAs(str(pdf_path), FileFormat=_WD_FORMAT_PDF)
            finally:
                doc.Close(False)  # 不保存改动
        finally:
            word.Quit()
    finally:
        pythoncom.CoUninitialize()


def _docx_to_pdf_soffice_sync(docx_path: Path, pdf_path: Path, soffice: str) -> None:
    """LibreOffice 无头转换：--convert-to pdf 落临时目录后归位。"""
    cfg = get_config()
    outdir = cfg.tmp_dir / f"soffice_{ULID()}"
    outdir.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(outdir), str(docx_path)],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=SOFFICE_TIMEOUT,
            check=False,  # 退出码在下方显式判断
        )
        produced = outdir / f"{docx_path.stem}.pdf"
        if proc.returncode != 0 or not produced.is_file():
            tail = ((proc.stderr or "") + (proc.stdout or "")).strip()[-400:]
            raise RuntimeError(f"soffice 退出码 {proc.returncode}：{tail or '无输出'}")
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(produced), str(pdf_path))
    finally:
        shutil.rmtree(outdir, ignore_errors=True)


def _humanize_com_error(exc: Exception) -> str:
    """把 pywin32 的 COM 异常元组压成一句人能读的话。

    win32com 抛的是 `(-2147352567, '发生意外。', (0, 'Microsoft Word', '真正的说明…'), None)`，
    直接 str() 会把整个元组连同 chm 帮助文件名一起塞给用户，噪音极大。
    """
    # excepinfo 形如 (wcode, source, description, helpfile, helpctx, scode)；
    # com_error.args 形如 (hresult, msg, excepinfo, argerr)，excepinfo 嵌在第 3 项。
    info = getattr(exc, "excepinfo", None)
    if not (isinstance(info, tuple) and len(info) >= 3):
        info = None
        for item in getattr(exc, "args", ()) or ():
            if isinstance(item, tuple) and len(item) >= 3 and isinstance(item[2], str):
                info = item
                break
    if info and isinstance(info[2], str) and info[2].strip():
        source = info[1] if len(info) > 1 and isinstance(info[1], str) and info[1].strip() else "Word"
        desc = " ".join(info[2].split())
        return f"{source}：{desc[:300]}"
    # 退而求其次：取 args 里第一段人类可读的文字
    for item in getattr(exc, "args", ()) or ():
        if isinstance(item, str) and item.strip():
            return " ".join(item.split())[:300]
    return str(exc)[:300]


def _engine_setting() -> str:
    """读 settings.general.pdf_engine（auto|word|soffice|pillow；异常回落 auto）。"""
    try:
        general = db.get_setting_json("general") or {}
        engine = str(general.get("pdf_engine") or "auto").lower()
        return engine if engine in ("auto", "word", "soffice", "pillow") else "auto"
    except Exception:  # noqa: BLE001
        return "auto"


async def docx_to_pdf(docx_path: Path, pdf_path: Path, engine: str | None = None) -> str:
    """docx → pdf。返回实际使用的引擎名（'word' | 'soffice'）。

    engine 缺省读 settings.general.pdf_engine；'auto' 按 word → soffice 依次尝试；
    全部失败抛 PdfExportError（消息聚合各引擎原因，明确指引）。
    """
    if not docx_path.is_file():
        raise PdfExportError(f"待转换的 Word 文件不存在（{docx_path.name}），请先重新生成 docx 再导出 PDF。")
    engine = (engine or _engine_setting()).lower()
    errors: list[str] = []

    # ① Word COM：专用线程 + 模块级锁串行
    if engine in ("auto", "word"):
        if _word_available():
            try:
                async with _word_lock:
                    await anyio.to_thread.run_sync(_docx_to_pdf_word_sync, docx_path, pdf_path)
                if pdf_path.is_file():
                    return "word"
                errors.append("Word 转换结束但没有生成 PDF 文件")
            except Exception as exc:  # noqa: BLE001  COM 异常种类繁杂，统一记录降级
                readable = _humanize_com_error(exc)
                errors.append(f"Word 转换失败（{readable}）")
                logger.warning("Word COM 转 PDF 失败：%s", exc)
        else:
            errors.append("本机未安装 Microsoft Word，或运行环境不是 Windows")

    # ② LibreOffice soffice
    if engine in ("auto", "soffice"):
        soffice = _soffice_path()
        if soffice:
            try:
                await anyio.to_thread.run_sync(_docx_to_pdf_soffice_sync, docx_path, pdf_path, soffice)
                return "soffice"
            except Exception as exc:  # noqa: BLE001
                errors.append(f"LibreOffice 转换失败（{str(exc)[:300]}）")
                logger.warning("soffice 转 PDF 失败：%s", exc)
        else:
            errors.append("本机未安装 LibreOffice（PATH 中找不到 soffice 命令）")

    # ③ 配置成了本模块不支持的引擎（Pillow 图片版兜底仅论文转专利模块可用）
    if engine not in ("auto", "word", "soffice"):
        raise PdfExportError(
            f"当前「设置 → PDF 引擎」配置为 {engine}，该引擎不支持 Word 文档转 PDF。"
            "请在设置中改回「自动」或选择 word / soffice；也可以只下载 docx 版本。"
        )

    # ④ 明确报错：说清楚每个引擎为什么不行，以及用户可以怎么办
    raise PdfExportError(
        "无法把 Word 文档转成 PDF：" + "；".join(errors) + "。"
        "解决办法：在本机安装 Microsoft Word 或 LibreOffice（安装后无需重启服务）；"
        "如果只是临时需要，可以直接下载 docx 版本，再用 Word 自行「另存为 PDF」。"
    )
