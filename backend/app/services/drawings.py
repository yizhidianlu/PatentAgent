"""附图生成服务（模块 B · B6）。

职责（backend-architecture.md §1.1 services/drawings.py）：
- 以子进程调 `app/tools/generate_patent_drawings.py --update-json`，由脚本据
  `drawings[]` 规格文字渲染黑白线条 SVG（+ Pillow PNG 兜底）并把
  `drawing_assets[] / image_model_prompts[] / drawing_validation[]` 回写进 JSON；
- 工作目录固定为 `data/outputs/{case_id}/p2p_work/`：脚本把 `svg_path/png_path`
  写成**相对 patent_content.json 所在目录**的路径，DOCX/PDF 生成器也按同一 base_dir
  解析，因此 JSON 与图片必须始终同目录（不要把 JSON 挪走）；
- 失败处置（prompt-porting-spec R13）：validation.passes=false → 调用方对该图规格
  REPAIR ≤2 次重跑；仍失败则 degrade_figure() 把该图降级为「只留 image_model_prompt」
  并产出一条 gaps 文案。

脚本本身零第三方依赖（PNG 需要 Pillow，缺失时自动只出 SVG），可在测试中真跑。
"""

from __future__ import annotations

import json
import base64
import logging
import re
import subprocess
from pathlib import Path
from typing import Any

import anyio.to_thread

from ..config import get_config
from . import paths as paths_service
from .convert import run_tool

logger = logging.getLogger(__name__)

# 附图脚本单次执行超时（秒）
DRAWINGS_TIMEOUT = 300

# 每案最多用图像模型补几张图。与本文件其余几处 REPAIR 上限同量级。
# 不设上限的话，「附图脚本整体失败」这一分支会对每张图各调一次，串行叠加。
MAX_AI_FIGURES = 3

# 工作目录内的内容 JSON 文件名（与图片同目录，供 DOCX/PDF 解析相对路径）
CONTENT_FILENAME = "patent_content.json"

# 脚本无法生成某图时的报错：Cannot generate figure 2: method steps are not explicit enough...
_FAILED_FIGURE_RE = re.compile(r"Cannot generate figure\s+(\d+)\s*:\s*(.*)", re.IGNORECASE)

# 脚本报出的「哪一类要素不够明确」→ 中文提示（措辞对齐 drawings[] 规格的写法要求）
_MISSING_KIND_HINT = {
    "method steps": "流程图需要能识别出至少 2 个步骤，规格里请写成「包含步骤S101，……；S102，……」的形式",
    "system modules": "结构示意图需要能识别出至少 2 个模块，规格里请写成「包含A模块、B模块、C模块」的形式",
    "data flow nodes": "数据流图需要能识别出至少 2 个节点，规格里请写成「数据流经A单元、B单元、C单元」的形式",
}


def _humanize_generator_error(combined: str, match: re.Match[str] | None) -> str:
    """把附图脚本的 Python 堆栈翻成「哪张图、为什么、该怎么改」的中文说明。"""
    if match:
        figure_no = match.group(1)
        rest = (match.group(2) or "").strip()
        hint = ""
        for key, text in _MISSING_KIND_HINT.items():
            if key in rest.lower():
                hint = text
                break
        if not hint:
            hint = "请把该图的规格写清楚：明确列出要画的步骤或模块名称，并用分号/顿号分隔"
        return f"图{figure_no} 无法生成：附图规格里的要素不够明确。{hint}。"
    tail = "；".join(ln.strip() for ln in combined.splitlines() if ln.strip())[-300:]
    return (
        "附图生成失败：脚本执行出错，附图规格可能不符合要求。"
        "请检查 drawings[] 中每条规格是否写明了图号与要画的步骤/模块。"
        + (f"（技术详情：{tail}）" if tail else "")
    )


class DrawingGenerationError(RuntimeError):
    """附图脚本执行失败（含无法从规格中识别步骤/模块的情形）。

    figure_no 为脚本报出的失败图号（解析不到时为 None）。
    """

    def __init__(self, message: str, figure_no: int | None = None) -> None:
        super().__init__(message)
        self.figure_no = figure_no


def work_dir(case_id: str) -> Path:
    """该案件的附图/文件生成工作目录（持久化，供 drawings→build 跨步骤复用）。"""
    path = get_config().outputs_dir / case_id / "p2p_work"
    path.mkdir(parents=True, exist_ok=True)
    return path


def content_path(case_id: str) -> Path:
    """工作目录内的 patent_content.json 路径。"""
    return work_dir(case_id) / CONTENT_FILENAME


def write_content_sync(case_id: str, content: dict[str, Any]) -> Path:
    """把内容 JSON 写进工作目录（UTF-8、不转义中文）。"""
    path = content_path(case_id)
    path.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def read_content_sync(case_id: str) -> dict[str, Any]:
    """回读工作目录里的内容 JSON。"""
    path = content_path(case_id)
    return json.loads(path.read_text(encoding="utf-8-sig"))


def run_generator_sync(
    case_id: str, content: dict[str, Any], *, prefix: str = "patent"
) -> dict[str, Any]:
    """同步执行附图脚本，返回回写后的内容 JSON（含 drawing_assets 等三组字段）。

    每次执行前丢弃旧的 drawing_assets，使脚本按当前 `drawings[]` 规格重新推导——
    规格 REPAIR 后重跑才能真正生效。
    """
    payload = dict(content)
    payload.pop("drawing_assets", None)
    json_path = write_content_sync(case_id, payload)
    out_dir = json_path.parent

    try:
        proc = run_tool(
            "generate_patent_drawings.py",
            [str(json_path), "--output-dir", str(out_dir), "--prefix", prefix, "--update-json"],
            timeout=DRAWINGS_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise DrawingGenerationError(
            f"附图生成超时（超过 {DRAWINGS_TIMEOUT} 秒）。附图数量或规格可能过于复杂，请减少附图数量后重试。"
        ) from exc
    except OSError as exc:
        raise DrawingGenerationError(
            f"无法启动附图生成程序（{exc}）。这是服务端环境问题，请联系维护者检查 Python 环境。"
        ) from exc

    if proc.returncode != 0:
        combined = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()
        m = _FAILED_FIGURE_RE.search(combined)
        raise DrawingGenerationError(
            _humanize_generator_error(combined, m),
            figure_no=int(m.group(1)) if m else None,
        )

    updated = read_content_sync(case_id)
    logger.info(
        "附图生成完成 case=%s 图数=%s", case_id, len(updated.get("drawing_assets") or [])
    )
    return updated


async def generate(
    case_id: str, content: dict[str, Any], *, prefix: str = "patent"
) -> dict[str, Any]:
    """run_generator_sync 的异步包装（子进程丢线程池）。"""
    return await anyio.to_thread.run_sync(
        lambda: run_generator_sync(case_id, content, prefix=prefix)
    )


# ---------------------------------------------------------------------------
# 校验结果与降级
# ---------------------------------------------------------------------------


def failed_figures(content: dict[str, Any]) -> list[int]:
    """validation.passes 不为真的图号列表（留白过大/图内混入图题等）。"""
    failed: list[int] = []
    for asset in content.get("drawing_assets") or []:
        if not isinstance(asset, dict):
            continue
        validation = asset.get("validation")
        ok = bool(validation.get("passes")) if isinstance(validation, dict) else False
        if not ok:
            failed.append(int(asset.get("figure_no") or 0))
    return [n for n in failed if n]


def spec_of(content: dict[str, Any], figure_no: int) -> str:
    """取某图当前的规格文字（优先 drawing_assets.spec，回落 drawings[] 中的对应行）。"""
    for asset in content.get("drawing_assets") or []:
        if isinstance(asset, dict) and int(asset.get("figure_no") or 0) == figure_no:
            spec = str(asset.get("spec") or "").strip()
            if spec:
                return spec
    for line in content.get("drawings") or []:
        text = str(line)
        m = re.search(r"图\s*(\d+)", text)
        if m and int(m.group(1)) == figure_no:
            return text.strip()
    return ""


def replace_spec(content: dict[str, Any], figure_no: int, new_spec: str) -> bool:
    """把 drawings[] 中某图的规格替换为 REPAIR 后的新规格；返回是否替换成功。"""
    drawings = content.get("drawings") or []
    for i, line in enumerate(drawings):
        m = re.search(r"图\s*(\d+)", str(line))
        if m and int(m.group(1)) == figure_no:
            drawings[i] = new_spec
            content["drawings"] = drawings
            return True
    return False


def fallback_image_prompt(figure_no: int, spec: str) -> str:
    """降级图的 Image2 精修提示词（措辞对齐脚本 image_model_prompt()，硬性约束一致）。"""
    clean = re.sub(r"^图\s*\d+\s*[：:]\s*", "", (spec or "").strip())
    return (
        f"请生成中国发明专利申请的说明书附图图{figure_no}，严格依据以下附图规格重绘：{clean}\n"
        "硬性要求：纯黑白线条图，纯白背景；不得使用彩色、灰度、渐变、阴影、照片、三维渲染、装饰元素、logo或水印；"
        "图片内部不得出现图号、附图标题、说明性段落或与技术方案无关的文字；"
        "仅保留说明书和权利要求支持的步骤编号、模块名称、关键术语、标号和箭头；"
        "主体内容应填充画布，目标内容宽度占比不低于0.80、高度占比不低于0.80，不得留下大面积空白；"
        "不得新增、删除或改变任何模块、步骤、连接关系和流程方向。"
    )


async def try_ai_figure(
    case_id: str, content: dict[str, Any], figure_no: int, *, step_key: str | None = None
) -> bool:
    """脚本画不出这张图时，用配置好的图像模型补一张；成功则写进 drawing_assets。

    此前这条路是断的：平台把精修提示词算好放进 image_model_prompts，却要用户自己
    拿去别处出图——设置页明明配了图像模型，却没有任何地方会用它。

    产物同时落 PNG 与一层 SVG 外壳。DOCX 生成器要求每个 asset 有 svg_path
    （png_path 只是可选回退），而它实际嵌入时优先用 PNG；给一层内嵌该 PNG 的 SVG，
    既满足了它的入口校验，嵌进文档的又是真正的图，无需改动交付路径。

    任何失败都只返回 False，由调用方降级为「只给提示词」——出图是增强，不是主路径。
    """
    from . import llm  # 延迟导入：drawings 被 CLI 工具单独引用时不该拖上 LLM 依赖
    from . import skills as skills_service

    # 技能开关要真的管用：这条会按图像模型计费，用户关掉它就是不想花这个钱。
    # 只改数据库而照样调用，等于在用户明确关闭之后继续扣他的额度。
    #
    # 用 is_user_enabled 而非 is_enabled：这里只需要知道「用户想不想用」。
    # 前置条件（有没有配图像模型）由下面的 generate_image 处理——
    # 它抛 ImageGenUnavailableError，同样落到静默降级，无须在这里重复判断。
    if not skills_service.is_user_enabled("ai_figure"):
        return False

    spec = spec_of(content, figure_no)
    if not spec:
        return False

    # 每案上限：整体失败分支会对每张图各调一次，而单次最坏要等满 IMAGE_TIMEOUT。
    # 八张图串起来就是二十多分钟，全压在一条用户正盯着的实时流水线上。
    # 本文件其余几处 REPAIR 都设了上限，这里没有理由例外。
    used = int(content.get("_ai_figure_count") or 0)
    if used >= MAX_AI_FIGURES:
        logger.info("AI 补图已达每案上限 %s，图%s 直接降级", MAX_AI_FIGURES, figure_no)
        return False

    prompt = fallback_image_prompt(figure_no, spec)
    try:
        data = await llm.generate_image(prompt, case_id=case_id, step_key=step_key)
    except llm.ImageGenUnavailableError:
        return False        # 没配就是没配，静默走降级
    except Exception as exc:  # noqa: BLE001
        logger.warning("AI 补图失败 case=%s 图%s：%s", case_id, figure_no, exc)
        content["_ai_figure_count"] = used + 1
        return False

    # 从这里到写回 content 全部包进 try：磁盘满、权限、路径过长都会抛 OSError，
    # 而 outputs/ 只增不改、会一直长大，磁盘满不是假设性风险。
    # 出图是增强能力，不该有本事让整条流水线失败。
    tmp_png = tmp_svg = None
    try:
        width, height = _png_size(data)
        work = work_dir(case_id)
        # 先写临时名，两个都成功再改名：中途失败会在磁盘上留下没人认领的 figure_N_ai.png
        tmp_png = work / f".figure_{figure_no}_ai.png.tmp"
        tmp_svg = work / f".figure_{figure_no}_ai.svg.tmp"
        tmp_png.write_bytes(data)
        tmp_svg.write_text(_svg_wrapper(data, width, height), encoding="utf-8")

        png_path = work / f"figure_{figure_no}_ai.png"
        svg_path = work / f"figure_{figure_no}_ai.svg"
        tmp_png.replace(png_path)
        tmp_svg.replace(svg_path)
        tmp_png = tmp_svg = None

        title = re.sub(r"^图\s*\d+\s*[：:]\s*", "", spec.strip())[:40] or f"图{figure_no}"
        assets = [
            a for a in (content.get("drawing_assets") or [])
            if not (isinstance(a, dict) and _figure_no_of(a) == figure_no)
        ]
        assets.append({
            "figure_no": figure_no,
            "svg_path": svg_path.name,
            "png_path": png_path.name,
            "title": title,
            "caption": f"图{figure_no} {title}",
            "source": "image_model",   # 标明来路：人工复核时要能一眼分出哪些图是模型画的
        })
        content["drawing_assets"] = sorted(assets, key=_figure_no_of)
    except Exception as exc:  # noqa: BLE001
        logger.warning("AI 补图落盘失败 case=%s 图%s：%s", case_id, figure_no, exc)
        for leftover in (tmp_png, tmp_svg):
            if leftover is not None:
                try:
                    leftover.unlink(missing_ok=True)
                except OSError:
                    pass
        content["_ai_figure_count"] = used + 1
        return False

    content["_ai_figure_count"] = used + 1
    logger.info("AI 补图成功 case=%s 图%s %sx%s", case_id, figure_no, width, height)
    return True


def _figure_no_of(asset: Any) -> int:
    """从 asset 里取图号；取不到就当 0。

    直接 int(a.get("figure_no") or 0) 会在遇到非数字时抛 ValueError——
    同一行已经防了 isinstance(a, dict)，却没防这个转换。
    """
    if not isinstance(asset, dict):
        return 0
    try:
        return int(asset.get("figure_no") or 0)
    except (TypeError, ValueError):
        return 0


def _png_size(data: bytes) -> tuple[int, int]:
    """从 PNG 字节读宽高（只认 IHDR，不引入图像库）。"""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("不是 PNG")
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def _svg_wrapper(png_data: bytes, width: int, height: int) -> str:
    """把 PNG 内嵌进一层 SVG：满足 DOCX 生成器对 svg_path 的入口校验。

    早先用相对路径 xlink:href 引同目录的 PNG，体积小，走 DOCX 那条路也没问题
    （生成器优先用 png_path）。但只要有人把这个 SVG 单独拿出来看——前端预览、
    或者只拷走 svg——就是一张裂图。内嵌 base64 换来的是「这个文件自己能站住」，
    代价是体积约为 PNG 的 1.37 倍，值得。
    """
    b64 = base64.b64encode(png_data).decode("ascii")
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width="{width}" height="{height}" viewBox="0 0 {width} {height}">\n'
        f'  <image xlink:href="data:image/png;base64,{b64}" '
        f'x="0" y="0" width="{width}" height="{height}"/>\n'
        '</svg>\n'
    )


def degrade_figure(content: dict[str, Any], figure_no: int, reason: str = "") -> str:
    """把某图降级为「只留 image_model_prompt」：移出 drawings/drawing_assets，返回 gaps 文案。

    降级后该图不再进入 DOCX 嵌图，但用户仍可拿提示词去图像模型自行精修。
    """
    spec = spec_of(content, figure_no)

    content["drawings"] = [
        line
        for line in (content.get("drawings") or [])
        if not _is_figure_line(str(line), figure_no)
    ]
    content["drawing_assets"] = [
        a
        for a in (content.get("drawing_assets") or [])
        if not (isinstance(a, dict) and int(a.get("figure_no") or 0) == figure_no)
    ]
    content["drawing_validation"] = [
        v
        for v in (content.get("drawing_validation") or [])
        if not (isinstance(v, dict) and int(v.get("figure_no") or 0) == figure_no)
    ]
    # 附图说明同步删除该图的一句话说明，避免说明书里出现无图的图号
    desc = content.get("description")
    if isinstance(desc, dict):
        desc["drawing_description"] = [
            line
            for line in (desc.get("drawing_description") or [])
            if not _is_figure_line(str(line), figure_no)
        ]

    prompts = [
        p
        for p in (content.get("image_model_prompts") or [])
        if not (isinstance(p, dict) and int(p.get("figure_no") or 0) == figure_no)
    ]
    prompts.append({"figure_no": figure_no, "prompt": fallback_image_prompt(figure_no, spec)})
    content["image_model_prompts"] = sorted(prompts, key=lambda p: int(p.get("figure_no") or 0))

    detail = f"（{reason}）" if reason else ""
    gap = (
        f"【待补充：图{figure_no} 未能由现有材料生成合规的黑白线条附图{detail}，"
        "已改为提供图像模型精修提示词，请人工绘制或用图像模型生成后补入。】"
    )
    gaps = list(content.get("gaps") or [])
    if gap not in gaps:
        gaps.append(gap)
    content["gaps"] = gaps
    return gap


def _is_figure_line(text: str, figure_no: int) -> bool:
    """该行文字是否描述指定图号（「图3：…」「图3为…」）。"""
    m = re.search(r"图\s*(\d+)", text)
    return bool(m) and int(m.group(1)) == figure_no


def asset_files(case_id: str, content: dict[str, Any]) -> list[dict[str, Any]]:
    """列出已生成的图片文件（绝对路径），供落 figure_svg / figure_png 交付物。

    顺便把 `media_path`（相对 DATA_DIR）回写进 `drawing_assets`。

    `png_path` 存的是**相对附图工作目录**的文件名，只有知道那个约定的人才解释得了。
    网页端要显示这张图，就得在另一侧再复刻一遍同样的约定——复刻出偏差的那一刻，
    图就不见了，而且一声不响。这里是唯一确知文件真实位置的地方（下面刚 `is_file()`
    验过），把一条自解释的路径落下来，两边就不必各猜各的。
    """
    base = work_dir(case_id)
    items: list[dict[str, Any]] = []
    for asset in content.get("drawing_assets") or []:
        if not isinstance(asset, dict):
            continue
        entry: dict[str, Any] = {"figure_no": int(asset.get("figure_no") or 0)}
        for key in ("svg_path", "png_path"):
            raw = str(asset.get(key) or "").strip()
            if not raw:
                continue
            path = Path(raw)
            if not path.is_absolute():
                path = (base / path).resolve()
            if path.is_file():
                entry[key] = path
                # 网页端取图用这一条；PNG 优先（Word 里嵌的就是它，所见即所得）
                if key == "png_path" or "media_path" not in asset:
                    asset["media_path"] = paths_service.to_stored(path)
        items.append(entry)
    return items
