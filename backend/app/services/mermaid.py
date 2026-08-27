"""Mermaid 代码 → PNG（M2）。

实现方式（backend-architecture.md §0 决策 2/3）：
- 子进程调 app/tools/mermaid_render.py（Playwright + vendored mermaid.min.js，
  浏览器顺序：本机 Chrome → Edge → 自带 Chromium，见 tools/browser.py）；
- 该脚本以整份 md 为输入 → 此处包一层适配：把单段 mermaid 代码写入临时 md
  （```mermaid 围栏），渲染后从 assets 目录取 fig_001.png；
- 机读协议：脚本 stderr 打印 "MERMAID: ok=<n> fail=<m>"；ok=0 即渲染失败；
- 渲染失败抛 MermaidRenderError（API 层转 422），错误详情取 stderr 关键行。
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path

import anyio.to_thread
from ulid import ULID

from ..config import get_config
from ..db import database as db
from .convert import run_tool

logger = logging.getLogger(__name__)

# 渲染超时（秒）：包含浏览器冷启动
RENDER_TIMEOUT = 180

_MERMAID_STAT_RE = re.compile(r"MERMAID:\s*ok=(\d+)\s+fail=(\d+)")


class MermaidRenderError(RuntimeError):
    """Mermaid 渲染失败（语法错误 / 浏览器不可用等）。"""


def _browser_channel_env() -> dict[str, str]:
    """从 settings.general.browser_channel 读浏览器通道，透传给 tools/browser.py。

    未配置时不设置环境变量，走脚本自动顺序（chrome → msedge → chromium）。
    """
    try:
        general = db.get_setting_json("general") or {}
        channel = str(general.get("browser_channel") or "").strip().lower()
        if channel in ("chrome", "msedge"):
            return {"PATENT_BROWSER_CHANNEL": channel}
    except Exception as exc:  # noqa: BLE001  DB 未初始化等场景不阻塞渲染
        logger.debug("读取 browser_channel 设置失败，走自动探测：%s", exc)
    return {}


# 脚本进度行：「已写入 <临时目录>（mermaid：0 处已转为 PNG…）」——对用户是纯噪音且会泄露服务端路径
_PROGRESS_LINE_RE = re.compile(r"^已写入\s")

# mermaid.js 常见英文报错 → 中文说明（用户看到的是自己写的图代码，得知道改哪儿）
_MERMAID_HINTS: tuple[tuple[str, str], ...] = (
    ("no diagram type detected", "第一行缺少图表类型声明，请以 flowchart TD / graph LR / sequenceDiagram 等开头"),
    ("parse error", "图表语法有误，请检查节点与箭头的写法（如 A[标题] --> B[标题]）"),
    ("lexical error", "图表里有 mermaid 不认识的字符，请检查中文标点或未闭合的括号"),
    ("无法启动浏览器", "服务端找不到可用的浏览器，请安装 Chrome/Edge，或在「设置」中指定浏览器通道"),
    ("executable doesn", "服务端找不到可用的浏览器，请安装 Chrome/Edge，或在「设置」中指定浏览器通道"),
)


def _extract_error(stderr: str) -> str:
    """从脚本 stderr 提炼人类可读的失败原因（中文说明优先，滤掉进度行与服务端路径）。"""
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    lines = [ln for ln in lines if not _PROGRESS_LINE_RE.match(ln)]
    hits = [ln for ln in lines if ("失败" in ln or "无法" in ln or "错误" in ln)]
    picked = hits or lines[-5:]
    raw = "；".join(picked)[-800:]
    # 掐掉脚本追加在错误后面的「已写入 <临时路径>（…）」尾巴
    raw = re.split(r"；?已写入\s", raw)[0].strip()
    low = raw.lower()
    for needle, hint in _MERMAID_HINTS:
        if needle in low or needle in raw:
            return f"{hint}。（原始报错：{raw[:300]}）"
    return raw or "未知错误，请检查 mermaid 图表代码是否完整"


def render_mermaid_png_sync(code: str, theme: str | None = None) -> bytes:
    """同步渲染：单段 mermaid 代码 → PNG bytes。失败抛 MermaidRenderError。"""
    code = (code or "").strip()
    if not code:
        raise MermaidRenderError("mermaid 代码为空")

    # theme 适配：脚本内 initialize 固定 theme=default，
    # 通过 mermaid 指令行（%%{init: ...}%%）覆盖；调用方已写指令则不重复加
    if theme and "%%{init" not in code:
        code = '%%{init: {"theme": "' + theme.replace('"', "") + '"}}%%\n' + code

    cfg = get_config()
    workdir = cfg.tmp_dir / f"mermaid_{ULID()}"
    workdir.mkdir(parents=True, exist_ok=True)
    in_md = workdir / "in.md"
    out_md = workdir / "out.md"
    in_md.write_text(f"```mermaid\n{code}\n```\n", encoding="utf-8")

    try:
        try:
            proc = run_tool(
                "mermaid_render.py",
                ["-i", str(in_md), "-o", str(out_md), "--no-docx", "--assets-dir", "figs"],
                timeout=RENDER_TIMEOUT,
                extra_env=_browser_channel_env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise MermaidRenderError(f"渲染超时（{RENDER_TIMEOUT}s）") from exc
        except OSError as exc:
            raise MermaidRenderError(f"无法启动渲染脚本：{exc}") from exc

        if proc.returncode != 0:
            raise MermaidRenderError(f"渲染脚本退出码 {proc.returncode}：{_extract_error(proc.stderr)}")

        # 机读协议：MERMAID: ok=<n> fail=<m>（脚本对失败块降级保留围栏、退出码仍为 0）
        m = _MERMAID_STAT_RE.search(proc.stderr or "")
        ok = int(m.group(1)) if m else 0
        png_path = workdir / "figs" / "fig_001.png"
        if ok < 1 or not png_path.is_file():
            raise MermaidRenderError(_extract_error(proc.stderr))
        return png_path.read_bytes()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def render_mermaid_png(code: str, theme: str | None = None) -> bytes:
    """异步包装：渲染丢线程池，避免阻塞事件循环。"""
    return await anyio.to_thread.run_sync(lambda: render_mermaid_png_sync(code, theme))


def render_md_file_sync(in_md: Path, out_md: Path, *, assets_dir: str = "mermaid_figures") -> dict:
    """整份 md 的 mermaid 围栏渲染（导出 docx 前置步骤用）。

    行为与脚本一致：单块失败保留围栏不中断；返回 {ok, fail}。
    整体失败（脚本崩溃 / 超时）抛 MermaidRenderError。
    """
    try:
        proc = run_tool(
            "mermaid_render.py",
            ["-i", str(in_md), "-o", str(out_md), "--no-docx", "--assets-dir", assets_dir],
            timeout=RENDER_TIMEOUT * 2,
            extra_env=_browser_channel_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise MermaidRenderError(f"渲染超时（{RENDER_TIMEOUT * 2}s）") from exc
    except OSError as exc:
        raise MermaidRenderError(f"无法启动渲染脚本：{exc}") from exc
    if proc.returncode != 0 or not out_md.is_file():
        raise MermaidRenderError(f"渲染脚本失败：{_extract_error(proc.stderr)}")
    m = _MERMAID_STAT_RE.search(proc.stderr or "")
    return {"ok": int(m.group(1)) if m else 0, "fail": int(m.group(2)) if m else 0}
