"""md → docx 导出（M2）。

实现方式（backend-architecture.md §5）：
- 若 md 含 ```mermaid 围栏：先经 services/mermaid.render_md_file_sync 生成 PNG
  （逐块降级：单块失败保留围栏，不中断）；
- 再子进程调 app/tools/md_to_docx.py（LaTeX 经 latex2mathml 转可编辑 OMML；
  失败留原文），机读协议：stderr 出现 "DOCX: ok=1" 即成功；
- 失败抛 DocxExportError（调用方决定 HTTP 语义）。

注意：mermaid 预渲染会把中间 md 写到 data/tmp/ 下的工作目录，原 md 内的
相对图片引用以「渲染后 md 所在目录」为 base——M2 的导出对象（交底书类 md）
图片均由 mermaid 渲染产生，暂无外部相对图片；M3 全链路会把 assets 落
outputs/{case_id}/ 一并管理。
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import anyio.to_thread
from ulid import ULID

from ..config import get_config
from .convert import run_tool
from .mermaid import MermaidRenderError, render_md_file_sync

logger = logging.getLogger(__name__)

# md→docx 超时（秒）
DOCX_TIMEOUT = 600

_MERMAID_FENCE_RE = re.compile(r"^```mermaid\s*$", re.IGNORECASE | re.MULTILINE)


class DocxExportError(RuntimeError):
    """md → docx 导出失败。"""


def export_md_to_docx_sync(md_path: Path, docx_path: Path) -> dict[str, Any]:
    """同步导出：md 文件 → docx 文件。返回统计 {mermaid_ok, mermaid_fail}。

    失败抛 DocxExportError；mermaid 预渲染整体失败仅降级（Word 中保留代码块）。
    """
    if not md_path.is_file():
        raise DocxExportError(
            f"待转换的 Markdown 文件不存在（{md_path.name}），无法导出 Word。"
            "请重新生成一次交付物后再导出。"
        )

    cfg = get_config()
    workdir = cfg.tmp_dir / f"export_{ULID()}"
    stats: dict[str, Any] = {"mermaid_ok": 0, "mermaid_fail": 0}
    src = md_path
    try:
        text = md_path.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            # 空正文导出会得到一份「打开是空白」的 Word，用户只会以为功能坏了
            raise DocxExportError(
                f"《{md_path.name}》没有正文内容，导出的 Word 会是一份空白文档。"
                "请先确认该交付物已生成完整内容，再导出。"
            )
        if _MERMAID_FENCE_RE.search(text):
            # 先渲染 mermaid 围栏为 PNG（写入 workdir；单块失败保留围栏）
            workdir.mkdir(parents=True, exist_ok=True)
            rendered = workdir / "rendered.md"
            try:
                r = render_md_file_sync(md_path, rendered)
                stats["mermaid_ok"], stats["mermaid_fail"] = r["ok"], r["fail"]
                src = rendered
            except MermaidRenderError as exc:
                # 整体渲染不可用（如无浏览器）→ 降级：直接转 docx，围栏以代码块出现
                logger.warning("mermaid 预渲染不可用，降级为源码入 Word：%s", exc)
                stats["mermaid_error"] = str(exc)

        docx_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            proc = run_tool(
                "md_to_docx.py",
                ["-i", str(src), "-o", str(docx_path), "--base-dir", str(src.parent)],
                timeout=DOCX_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            raise DocxExportError(
                f"生成 Word 文档超时（超过 {DOCX_TIMEOUT} 秒）。文档可能过长或图表过多，"
                "请拆分后分批导出，或稍后重试。"
            ) from exc
        except OSError as exc:
            raise DocxExportError(
                f"无法启动 Word 生成程序（{exc}）。这是服务端环境问题，请联系维护者检查 Python 环境。"
            ) from exc

        # 机读协议：stderr 出现 "DOCX: ok=1" 且退出码 0 才算成功
        stderr = proc.stderr or ""
        if proc.returncode != 0 or "DOCX: ok=1" not in stderr:
            tail = "；".join(ln.strip() for ln in stderr.splitlines() if ln.strip())[-500:]
            raise DocxExportError(
                "生成 Word 文档失败：正文中可能含有本工具无法处理的内容（如异常的表格或公式）。"
                "可以先下载 Markdown 版本自行转换，或修改后重试。"
                + (f"（技术详情：{tail}）" if tail else "")
            )
        if not docx_path.is_file():
            raise DocxExportError(
                "生成 Word 文档失败：程序报告成功但磁盘上没有产出文件，"
                "请检查数据目录是否有写入权限或磁盘是否已满。"
            )

        # 附带公式去向统计（MATH: omml=.. png=.. text=..），供调用方提示
        m = re.search(r"MATH:\s*omml=(\d+)\s+png=(\d+)\s+text=(\d+)", stderr)
        if m:
            stats["math"] = {"omml": int(m.group(1)), "png": int(m.group(2)), "text": int(m.group(3))}
        return stats
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def export_md_to_docx(md_path: Path, docx_path: Path) -> dict[str, Any]:
    """异步包装：导出丢线程池执行。"""
    return await anyio.to_thread.run_sync(lambda: export_md_to_docx_sync(md_path, docx_path))
