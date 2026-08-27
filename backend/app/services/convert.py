"""上传文件转换调度（M2）。

职责（backend-architecture.md §1.1 services/convert.py）：
- .docx → md：子进程调 app/tools/docx_to_md.py（mammoth；stdout「已写入/图片目录」，以退出码为准）；
- .pptx/.ppsx → md：子进程调 app/tools/pptx_to_md.py（python-pptx）；
- .pdf → md：直接 import pymupdf 抽取文本 + 内嵌图片到 uploads/{case_id}/figures/，
  记录页码与图注候选（"图1 …"/"Figure 1 …"等行）；
- 图片 / 代码 / md / txt：直存（文本类 md_path 指向原件，供 /files/{id}/content 读取）；
- 任何转换失败都不抛致命错误：错误写入返回 meta 的 convert_error，由 API 层落 files.meta_json。

convert_error 的写法准则（用户核心痛点是「上传后功能不可用却不知道为什么」）：
必须是中文，说清「哪个文件、哪里出错、该怎么办」，绝不把 Python 堆栈或服务端绝对路径
直接回显；技术细节只作为末尾的「（技术详情：…）」附注。凡是 md_path 为 None 的路径，
下游 pipeline 都会拿 convert_error 原样回显给用户，所以这段话就是用户看到的全部信息。

「转换成功但没有内容」同样按失败处理（md_path=None + convert_error）：
无文字层的扫描件 PDF、空白 docx、0 字节文件——静默产出空 md 会让用户以为系统坏了。

目录约定（data/uploads/{case_id}/）：
- 原件      uploads/{case_id}/{原名（去非法字符，重名自动加 _n）}
- 转换 md   uploads/{case_id}/converted/{原名主干}.md
- 抽取图片  docx/pptx 用脚本默认的 converted/{主干}_media/；pdf 落 uploads/{case_id}/figures/{主干}/
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import get_config

logger = logging.getLogger(__name__)

# app/tools 目录（移植的 CLI 脚本所在处）
TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"

# 单次子进程转换超时（秒）
CONVERT_TIMEOUT = 300

# 扩展名分类
_DOCX_EXTS = {".docx"}
_PPTX_EXTS = {".pptx", ".ppsx"}
_PDF_EXTS = {".pdf"}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".tif", ".tiff"}
_TEXT_EXTS = {
    ".md", ".markdown", ".txt", ".json", ".yaml", ".yml", ".csv", ".xml", ".html",
    ".py", ".go", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".rs", ".sql",
}

# 旧版 Office 二进制格式（OLE2 复合文档）：本机无 Word/PowerPoint 也能读的库不存在，
# 明确告诉用户「另存为新格式」而不是含糊地「仅存原件」。
_LEGACY_OFFICE_HINT = {
    ".doc": ("Word 97-2003 文档（.doc）", "Word", ".docx"),
    ".dot": ("Word 97-2003 模板（.dot）", "Word", ".docx"),
    ".ppt": ("PowerPoint 97-2003 演示文稿（.ppt）", "PowerPoint", ".pptx"),
    ".pps": ("PowerPoint 97-2003 放映（.pps）", "PowerPoint", ".pptx"),
    ".pot": ("PowerPoint 97-2003 模板（.pot）", "PowerPoint", ".pptx"),
    ".xls": ("Excel 97-2003 工作簿（.xls）", "Excel", ".xlsx"),
    ".wps": ("WPS 旧版文字文档（.wps）", "WPS 文字", ".docx"),
}

# 图注候选行：图1 / 附图 2 / 表3 / Figure 4 / Fig. 5 / Table 6
_CAPTION_RE = re.compile(r"^(图|附图|表|figure|fig\.?|table)\s*\d+", re.IGNORECASE)

# Windows 文件名非法字符
_ILLEGAL_NAME_RE = re.compile(r'[\\/:*?"<>|\r\n\t]')

# 单个文件名最长字符数。Windows 单路径段上限 255，但 data/uploads/{case_id}/converted/
# 之下还要再套 `{主干}_media/` 与图片名，整条路径易破 MAX_PATH(260)，故收紧到 80。
MAX_FILENAME_LEN = 80


def run_tool(
    script: str,
    args: list[str],
    *,
    timeout: int = CONVERT_TIMEOUT,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """以当前 venv 解释器子进程运行 app/tools 下的 CLI 脚本。

    - 强制 UTF-8 stdio（脚本自身也有 ensure_utf8_stdio，双保险）；
    - cwd 设为 TOOLS_DIR，保证脚本内部 `import stdio_utf8` 等同目录导入可用；
    - 非零退出码不抛异常，由调用方按需处理（转换失败非致命）；
    - 超时抛 subprocess.TimeoutExpired，由调用方捕获。
    """
    cmd = [sys.executable, str(TOOLS_DIR / script), *args]
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        cmd,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=str(TOOLS_DIR),
        timeout=timeout,
        check=False,  # 非零退出码由调用方判断（转换失败非致命）
    )


def sanitize_filename(name: str) -> str:
    """清洗上传文件名：仅取 basename、去非法字符、防空名、限长。

    限长很关键：期刊下载的论文 PDF 文件名动辄 150+ 字符，直接落盘会在 Windows 上
    因整条路径超 MAX_PATH 抛 FileNotFoundError，把整个上传请求打成 500。
    截断时保留扩展名，主干过长则从尾部截。
    """
    base = Path(name).name
    # 只去尾部的点（Windows 不允许文件名以点结尾）；前导点要留，否则 `....md`/`.gitignore`
    # 会被削成无扩展名的怪名字，进而误判为「不支持的格式」
    base = _ILLEGAL_NAME_RE.sub("", base).strip().rstrip(". ")
    if not base or set(base) <= {"."}:
        return "unnamed"
    if len(base) <= MAX_FILENAME_LEN:
        return base
    suffix = Path(base).suffix
    # 扩展名本身就离谱长时直接丢弃，避免截断后只剩扩展名
    if len(suffix) > 16:
        suffix = ""
    stem = base[: len(base) - len(suffix)] if suffix else base
    stem = stem[: max(1, MAX_FILENAME_LEN - len(suffix))]
    return (stem + suffix) or "unnamed"


def allocate_upload_path(case_dir: Path, orig_name: str) -> Path:
    """在案件上传目录内为原件分配不冲突的落盘路径（重名自动 _1/_2…）。"""
    safe = sanitize_filename(orig_name)
    stem, suffix = Path(safe).stem, Path(safe).suffix
    candidate = case_dir / safe
    n = 0
    while candidate.exists():
        n += 1
        candidate = case_dir / f"{stem}_{n}{suffix}"
    return candidate


@dataclass
class ConvertResult:
    """一次转换的结果：md 路径（可无）+ 元信息（含 convert_error）。"""

    md_path: Path | None = None
    meta: dict[str, Any] = field(default_factory=dict)


def _tail(text: str, limit: int = 800) -> str:
    """取文本尾部（错误详情用，避免超长入库）。"""
    text = (text or "").strip()
    return text[-limit:] if len(text) > limit else text


# 转换产物里由本工具自己生成的骨架：元信息注释、分页标题、图片引用
_MD_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_MD_PAGE_HEADING_RE = re.compile(r"^\s*#{1,6}\s*第\s*\d+\s*页\s*$", re.MULTILINE)
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")


def _body_text(md_text: str) -> str:
    """剥掉转换器自造的骨架后剩下的正文（用于判断「转出来其实是空的」）。"""
    text = _MD_COMMENT_RE.sub("", md_text or "")
    text = _MD_PAGE_HEADING_RE.sub("", text)
    text = _MD_IMAGE_RE.sub("", text)
    return text.strip()


def _detail(text: str, limit: int = 200) -> str:
    """把子进程 stderr 压成一行技术详情（追加在中文说明之后，供排查用）。"""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    # 优先取最后一行的异常摘要（形如 `zipfile.BadZipFile: File is not a zip file`）
    return lines[-1][:limit]


def _humanize_script_error(kind: str, orig_name: str, stderr: str) -> str:
    """把 docx/pptx 转换脚本的 Python 堆栈翻成用户能照做的中文说明。

    kind 为「Word」或「PowerPoint」；返回「原因 + 该怎么办（+ 技术详情）」。
    """
    text = stderr or ""
    new_ext = ".docx" if kind == "Word" else ".pptx"
    detail = _detail(text)
    suffix = f"（技术详情：{detail}）" if detail else ""

    if "BadZipFile" in text or "not a zip file" in text or "File is not a zip file" in text:
        return (
            f"《{orig_name}》不是有效的 {new_ext} 文件：它可能已损坏，"
            f"也可能本是旧版 .{new_ext[1:-1]}（97-2003 二进制格式）或其它格式被改成了 {new_ext} 扩展名。"
            f"请用 {kind} 打开原文件后「另存为」{new_ext} 再上传。{suffix}"
        )
    if "缺少依赖" in text or "ModuleNotFoundError" in text or "ImportError" in text:
        return (
            f"服务端缺少解析 {new_ext} 所需的依赖库，无法转换《{orig_name}》。"
            f"请联系维护者执行 pip install -r requirements.txt 后重试。{suffix}"
        )
    if "PackageNotFoundError" in text or "no relationship" in text.lower():
        return (
            f"《{orig_name}》的 {new_ext} 内部结构不完整（缺少必需的组成部分），无法解析。"
            f"请用 {kind} 打开并重新另存为 {new_ext} 后上传。{suffix}"
        )
    if "Permission" in text or "拒绝访问" in text:
        return (
            f"读取《{orig_name}》被系统拒绝：该文件可能正被 {kind} 打开占用。"
            f"请关闭 {kind} 后重新上传。{suffix}"
        )
    if "MemoryError" in text:
        return f"《{orig_name}》体积过大，转换时内存不足。请拆分为多个较小的文件后分别上传。{suffix}"
    return (
        f"《{orig_name}》转换失败：{kind} 文件无法被解析，通常是文件损坏或格式不受支持。"
        f"请用 {kind} 打开并另存为 {new_ext} 后重试；若仍失败，可改为上传导出的 PDF 或纯文本。{suffix}"
    )


def _script_convert(script: str, stored_path: Path, out_md: Path) -> ConvertResult:
    """docx/pptx → md：子进程调对应脚本；失败记 convert_error 不抛。"""
    out_md.parent.mkdir(parents=True, exist_ok=True)
    meta: dict[str, Any] = {}
    kind = "Word" if script.startswith("docx") else "PowerPoint"
    name = stored_path.name
    try:
        proc = run_tool(script, ["-i", str(stored_path), "-o", str(out_md)])
    except subprocess.TimeoutExpired:
        meta["convert_error"] = (
            f"《{name}》转换超时（超过 {CONVERT_TIMEOUT} 秒仍未完成）。"
            "该文件可能过大或内含超多图片，请拆分后分批上传。"
        )
        return ConvertResult(meta=meta)
    except OSError as exc:
        meta["convert_error"] = (
            f"无法启动 {kind} 转换程序（{exc}）。这是服务端环境问题，请联系维护者检查 Python 环境。"
        )
        return ConvertResult(meta=meta)

    if proc.returncode != 0:
        meta["convert_error"] = _humanize_script_error(kind, name, proc.stderr)
        return ConvertResult(meta=meta)
    if not out_md.is_file():
        meta["convert_error"] = (
            f"《{name}》转换程序已结束但没有产出文本，可能是文件内容为空或结构异常。"
            f"请确认用 {kind} 能正常打开该文件。"
            + (f"（技术详情：{_detail(proc.stderr)}）" if _detail(proc.stderr) else "")
        )
        return ConvertResult(meta=meta)

    # 脚本默认把图片抽到「{md 主干}_media/」；记录以便删除时清理
    media_dir = out_md.parent / f"{out_md.stem}_media"
    if media_dir.is_dir():
        meta["media_dir"] = str(media_dir)
        meta["figure_count"] = sum(1 for p in media_dir.iterdir() if p.is_file())
    return ConvertResult(md_path=out_md, meta=meta)


def _image_check(stored_path: Path, suffix: str) -> ConvertResult:
    """图片直存 + 完整性校验：损坏的图片当场说清楚，别等到嵌进附图时才炸。"""
    meta: dict[str, Any] = {"category": "image"}
    name = stored_path.name
    if suffix == ".svg":
        # 矢量图不走 Pillow：做一次极轻量的 <svg> 标签存在性检查即可
        try:
            head = stored_path.read_bytes()[:4096].decode("utf-8", errors="replace").lower()
        except OSError:
            return ConvertResult(meta=meta)
        if "<svg" not in head:
            meta["convert_error"] = (
                f"《{name}》的扩展名是 .svg，但内容不是有效的 SVG 矢量图。"
                "请确认文件未损坏，或改为上传 PNG/JPG 位图。"
            )
        return ConvertResult(meta=meta)

    try:
        from PIL import Image
    except ImportError:  # pragma: no cover —— Pillow 是硬依赖，缺失时不因校验而拦截上传
        return ConvertResult(meta=meta)

    try:
        with Image.open(stored_path) as im:
            im.verify()
        with Image.open(stored_path) as im:  # verify() 后需重开才能读属性
            meta["image_size"] = list(im.size)
            meta["image_format"] = im.format
    except Exception as exc:  # noqa: BLE001  Pillow 的损坏图异常种类很多
        meta["convert_error"] = (
            f"《{name}》不是可以打开的图片：文件可能在传输中损坏，或扩展名与实际格式不符。"
            f"请用看图软件确认能正常打开后重新上传。（技术详情：{type(exc).__name__}）"
        )
    return ConvertResult(meta=meta)


def _humanize_pdf_open_error(orig_name: str, exc: Exception) -> str:
    """把 pymupdf 的英文打开异常翻成中文说明（并且不回显服务端绝对路径）。"""
    raw = str(exc)
    low = raw.lower()
    if "encrypted" in low or "password" in low:
        return (
            f"《{orig_name}》已加密（打开时需要密码），无法读取内容。"
            "请先用 PDF 阅读器（如 Adobe Acrobat、福昕）输入密码后另存一份无密码的 PDF 再上传。"
        )
    if "no objects found" in low or "cannot open" in low or "failed to open" in low:
        return (
            f"《{orig_name}》不是有效的 PDF：文件可能在下载或传输中损坏，也可能本是其它格式被改成了 .pdf 扩展名。"
            "请用 PDF 阅读器确认能正常打开后重新上传。"
        )
    return (
        f"《{orig_name}》解析失败：PDF 内部结构异常，无法提取内容。"
        f"请尝试用 PDF 阅读器「另存为 / 打印为 PDF」重新生成一份后上传。（技术详情：{raw[:200]}）"
    )


def _pdf_convert(case_dir: Path, stored_path: Path, out_md: Path) -> ConvertResult:
    """PDF → md：pymupdf 直接抽文本；内嵌图片落 figures/{主干}/；记录页码与图注候选。"""
    meta: dict[str, Any] = {}
    name = stored_path.name
    try:
        import pymupdf
    except ImportError as exc:  # 依赖缺失按转换失败处理，不致命
        meta["convert_error"] = (
            f"服务端缺少 PDF 解析库 pymupdf，无法转换《{name}》。"
            f"请联系维护者执行 pip install -r requirements.txt 后重试。（技术详情：{exc}）"
        )
        return ConvertResult(meta=meta)

    figures_dir = case_dir / "figures" / stored_path.stem
    out_md.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [f"<!-- 由 convert.py 自 {name} 抽取，勿手改本行元信息 -->\n"]
    captions: list[dict[str, Any]] = []
    figures: list[dict[str, Any]] = []
    seen_xrefs: set[int] = set()
    text_chars = 0

    try:
        with pymupdf.open(str(stored_path)) as doc:
            # 加密 PDF：pymupdf 能开出 doc 但取文本时才炸，此处提前给出可操作的提示
            if getattr(doc, "needs_pass", False):
                meta["encrypted"] = True
                meta["convert_error"] = (
                    f"《{name}》已加密（打开时需要密码），无法读取内容。"
                    "请先用 PDF 阅读器输入密码打开后另存一份无密码的 PDF 再上传。"
                )
                return ConvertResult(meta=meta)
            meta["pages"] = doc.page_count
            for pno, page in enumerate(doc, start=1):
                text = (page.get_text("text") or "").strip()
                text_chars += len(text)
                lines.append(f"\n## 第 {pno} 页\n")
                if text:
                    lines.append(f"\n{text}\n")
                # 图注候选：形如「图1 xxx」「Figure 2 xxx」的行，记录页码
                for raw in text.splitlines():
                    stripped = raw.strip()
                    if stripped and _CAPTION_RE.match(stripped):
                        captions.append({"page": pno, "text": stripped[:120]})
                # 抽取内嵌图片（同一 xref 跨页复用时只存一份）
                for idx, info in enumerate(page.get_images(full=True), start=1):
                    xref = int(info[0])
                    if xref in seen_xrefs:
                        continue
                    seen_xrefs.add(xref)
                    try:
                        img = doc.extract_image(xref)
                        ext = (img.get("ext") or "png").lower()
                        figures_dir.mkdir(parents=True, exist_ok=True)
                        fname = f"p{pno:03d}_img{idx:02d}_x{xref}.{ext}"
                        fpath = figures_dir / fname
                        fpath.write_bytes(img["image"])
                        rel = os.path.relpath(fpath, out_md.parent).replace("\\", "/")
                        lines.append(f"\n![第{pno}页图{idx}]({rel})\n")
                        figures.append({"page": pno, "path": str(fpath)})
                    except Exception as exc:  # noqa: BLE001  单图失败不影响整体
                        logger.warning("PDF 图片抽取失败（第 %s 页 xref=%s）：%s", pno, xref, exc)
    except Exception as exc:  # noqa: BLE001  损坏 PDF 等
        meta["convert_error"] = _humanize_pdf_open_error(name, exc)
        return ConvertResult(meta=meta)

    pages = int(meta.get("pages") or 0)
    meta["text_chars"] = text_chars
    meta["figure_captions"] = captions
    meta["figures"] = figures
    if figures:
        meta["media_dir"] = str(figures_dir)

    # 扫描件 / 图片版 PDF：一个字都抽不出来。必须显式告知，
    # 否则用户只会看到「上传成功」却发现后续功能全部不可用。
    if pages > 0 and text_chars == 0:
        if figures:
            meta["convert_error"] = (
                f"《{name}》没有可提取的文字层（共 {pages} 页，疑似扫描件或图片版 PDF）。"
                f"已从中抽出 {len(figures)} 张页面图片留作附图素材，但正文文字为空，无法用于后续生成。"
                "请改用带文字层的原始 PDF（如从 Word 直接导出的版本）；"
                "若只有扫描件，请先用 OCR 工具（Adobe Acrobat、ABBYY FineReader、WPS 的「PDF 转 Word」等）"
                "转成可搜索 PDF 或 Word 后再上传。"
            )
        else:
            meta["convert_error"] = (
                f"《{name}》共 {pages} 页，但既没有提取到任何文字，也没有提取到任何图片，"
                "该 PDF 可能是空白页或内容以特殊方式嵌入。"
                "请确认文件内容正常；若为扫描件，请先做 OCR 转成可搜索 PDF 后再上传。"
            )
        meta["scanned"] = True
        # 不产出 md：下游流水线一律按「无可用文本」拒绝，并回显上面这段说明
        return ConvertResult(meta=meta)

    # 文字极其稀疏（多页平均不足 15 字）：多半是图文混排的扫描件，给出提醒但不阻断
    if pages >= 3 and text_chars < pages * 15:
        meta["convert_warning"] = (
            f"《{name}》共 {pages} 页但只提取到约 {text_chars} 个字符，正文可能大部分是图片（扫描件）。"
            "如果后续生成的内容明显缺失，请改用带完整文字层的 PDF 或先做 OCR。"
        )

    out_md.write_text("".join(lines).rstrip() + "\n", encoding="utf-8")
    return ConvertResult(md_path=out_md, meta=meta)


def convert_upload(case_id: str, stored_path: Path) -> ConvertResult:
    """按扩展名调度转换。任何失败都体现在 meta['convert_error']，绝不抛致命错误。

    返回的 meta 至少含 digest；docx/pptx/pdf 成功时含 md 相关信息。
    """
    cfg = get_config()
    case_dir = cfg.uploads_dir / case_id
    suffix = stored_path.suffix.lower()
    name = stored_path.name
    out_md = case_dir / "converted" / f"{stored_path.stem}.md"

    # 0 字节文件：转换必然产出空内容，提前拦下并说清楚（多半是拖拽了未下载完的文件）
    try:
        if stored_path.stat().st_size == 0:
            return ConvertResult(
                meta={
                    "convert_error": (
                        f"《{name}》是空文件（0 字节），没有任何内容可读取。"
                        "请确认文件已完整下载/保存后重新上传。"
                    )
                }
            )
    except OSError:
        pass

    try:
        if suffix in _DOCX_EXTS:
            result = _script_convert("docx_to_md.py", stored_path, out_md)
        elif suffix in _PPTX_EXTS:
            result = _script_convert("pptx_to_md.py", stored_path, out_md)
        elif suffix in _PDF_EXTS:
            result = _pdf_convert(case_dir, stored_path, out_md)
        elif suffix in _LEGACY_OFFICE_HINT:
            # 旧版 Office 二进制格式：明确说「不支持 + 怎么办」，而不是含糊的「仅存原件」
            label, app_name, new_ext = _LEGACY_OFFICE_HINT[suffix]
            result = ConvertResult(
                meta={
                    "category": "legacy_office",
                    "convert_error": (
                        f"《{name}》是{label}，本系统不支持解析这种旧版二进制格式。"
                        f"请用 {app_name}（或 WPS）打开该文件，选择「另存为」→ 保存类型选 {new_ext}，"
                        f"再上传新生成的 {new_ext} 文件。原件已保留，可随时下载。"
                    ),
                }
            )
        elif suffix in _IMAGE_EXTS:
            result = _image_check(stored_path, suffix)
        elif suffix in _TEXT_EXTS:
            # 文本 / 代码 / md：直存，md_path 指向原件（/files/{id}/content 可直接读）
            result = ConvertResult(md_path=stored_path, meta={"category": "text"})
        else:
            result = ConvertResult(
                meta={
                    "category": "other",
                    "note": f"暂不支持 {suffix or '无扩展名'} 的文本转换，仅存原件",
                    "convert_error": (
                        f"《{name}》的格式（{suffix or '无扩展名'}）暂不支持提取文本，已作为附件原样保存。"
                        "如需让系统读取其中内容，请转存为 PDF、Word（.docx）、PowerPoint（.pptx）、"
                        "Markdown 或纯文本后再上传。"
                    ),
                }
            )
    except Exception as exc:  # 兜底：调度层任何意外都转为 convert_error
        logger.exception("转换 %s 时发生未预期异常", name)
        result = ConvertResult(
            meta={
                "convert_error": (
                    f"《{name}》转换时发生未预期的内部错误，原件已保留。"
                    f"请重试一次；若持续失败请联系维护者并附上此信息。（技术详情：{exc}）"
                )
            }
        )

    # 统一补充摘要信息
    try:
        result.meta.setdefault("digest", hashlib.sha256(stored_path.read_bytes()).hexdigest())
    except OSError:
        pass
    if result.md_path is not None and result.md_path != stored_path:
        # 转换产物「实质为空」也视为失败（保留空文件便于排查）：
        # 只剩本工具自己写的元信息注释 / 分页标题 / 图片引用时，对用户等同于没转出东西。
        try:
            raw = result.md_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            result.meta.setdefault(
                "convert_error",
                f"《{name}》转换产物无法读取（{exc}）。请重新上传该文件。",
            )
            result.md_path = None
        else:
            if not _body_text(raw):
                result.meta.setdefault(
                    "convert_error",
                    f"《{name}》可以打开，但里面没有任何可提取的文字内容（转换结果为空）。"
                    "请确认该文件确实含有文本；若正文都是图片或艺术字，请改为上传含文字的版本。",
                )
                result.md_path = None
    return result
