#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate black-and-white SVG patent reference drawings from structured JSON.

The script uses only the Python standard library. It is intentionally template
based: drawings must be faithful to the patent text and should not introduce
new technical modules or steps.
"""

from __future__ import annotations


import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape


FIGURE_RE = re.compile(r"图\s*(\d+)")
STEP_RE = re.compile(r"(S\d{3})[，,:：、\s]*(.*?)(?=；|;|。|$)")
CONTENT_RATIO_TARGET = 0.80
CONTENT_RATIO_FLOOR = 0.78


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("Input JSON must be an object.")
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def as_paragraphs(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(as_paragraphs(item))
        return result
    if isinstance(value, dict):
        result: list[str] = []
        for item in value.values():
            result.extend(as_paragraphs(item))
        return result
    text = str(value).strip()
    if not text:
        return []
    return [part.strip() for part in re.split(r"(?:\r?\n){1,}", text) if part.strip()]


def xml(text: str) -> str:
    return escape(text, {'"': "&quot;"})


def default_prefix(input_path: Path) -> str:
    stem = input_path.stem
    for suffix in ("_patent_content", "_专利申请书"):
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem


def compact_label(text: str, limit: int = 28) -> str:
    label = re.sub(r"\s+", "", text)
    label = re.sub(r"[。；;，,：:]+$", "", label)
    if len(label) <= limit:
        return label
    return label[: limit - 1] + "…"


def has_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text))


#: 一个方框最多排几行。超过就不是图了，是段落——那时该让模型把步骤写短，
#: 而不是让脚本把句子剁断。
MAX_LABEL_LINES = 5

#: 非汉字（拉丁字母 / 数字）相对字号的宽度系数。等宽衬线里 ASCII 约为半角，
#: 取 0.56 略留富余：宁可一行少放一个字，也不要让字压到框线上。
ASCII_WIDTH_RATIO = 0.56

#: 不允许在中间断开的连续片段：模型名、缩写、编号。
#: 「MobileNetV4」被断成「MobileNetV / 4」在专利附图里就是个错字。
_ATOM_RE = re.compile(r"[A-Za-z0-9]+(?:[.+-][A-Za-z0-9]+)*")


def char_width(ch: str, font_size: int) -> float:
    """单字符的估算宽度。汉字与全角标点按一个字宽，拉丁与数字按半角。"""
    if has_cjk(ch) or ch in "，。、；：！？（）「」【】—…":
        return float(font_size)
    return font_size * ASCII_WIDTH_RATIO


def text_width(text: str, font_size: int) -> float:
    return sum(char_width(ch, font_size) for ch in text)


def _atoms(text: str) -> list[str]:
    """把文本切成「不可再断的最小单位」：一个汉字，或一整段拉丁/数字。"""
    out: list[str] = []
    pos = 0
    for m in _ATOM_RE.finditer(text):
        out.extend(text[pos:m.start()])
        out.append(m.group(0))
        pos = m.end()
    out.extend(text[pos:])
    return out


def wrap_label(
    text: str,
    max_width: float,
    font_size: int = 24,
    max_lines: int = MAX_LABEL_LINES,
) -> list[str]:
    """把标签折成能装进 `max_width` 的若干行。

    **按估算宽度折行，不按字符个数。**早先是数字符个数（写死 12 个），
    带来两个问题：一是 ASCII 只有半角，按个数算会让含模型名的行白白空掉半行；
    二是布局算高度和渲染画字用的是两个不同的 max_chars，
    于是**算出来的盒子装不下真正排出来的行数**，字压到框线外面去。
    现在布局与两个渲染器共用这一个函数、同一组参数。

    另外早先是 `return lines[:3]`：多出来的行**直接丢掉，一声不吭**。
    配合上游 20 字的硬截断，一条完整步骤到了图上就变成
    「S101 获取视频喉镜在气管插管场景中采集的喉镜…」——句子断在半途。
    专利附图是要交到审查员手里的，这种图不能用。
    """
    text = text.strip()
    if not text:
        return [""]
    lines: list[str] = []
    current = ""
    for atom in _atoms(text):
        if current and text_width(current + atom, font_size) > max_width:
            lines.append(current)
            current = atom
        else:
            current += atom
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        tail = lines[-1]
        while tail and text_width(tail + "…", font_size) > max_width:
            tail = tail[:-1]
        lines[-1] = tail + "…"
    return lines


#: 图题最多留多少字。图题会出现在附图下方与说明书里，太长不好看，
#: 但 28 字（原值）连一句完整的图题都放不下——用户看到的就是一串「…」。
CAPTION_LIMIT = 60


def title_from_spec(spec: str, figure_no: int) -> str:
    """从规格里取图题。

    优先切到第一个句读处，让图题在**语义边界**结束，而不是在第 28 个字上切一刀。
    实在没有句读才按长度兜底。
    """
    first = as_paragraphs(spec)[0] if as_paragraphs(spec) else spec
    first = re.sub(r"^图\s*\d+\s*[：:]\s*", "", first).strip()
    first = re.sub(r"^图\s*\d+\s*为\s*", "", first).strip()
    if len(first) > CAPTION_LIMIT:
        # 在限长之内找最后一个句读，切在那里
        head = first[:CAPTION_LIMIT]
        cut = max(head.rfind(ch) for ch in "，,；;。")
        if cut >= CAPTION_LIMIT // 3:
            first = head[:cut]
    return compact_label(first or f"图{figure_no}", CAPTION_LIMIT)


def figure_no_from_spec(spec: str, fallback: int) -> int:
    match = FIGURE_RE.search(spec)
    return int(match.group(1)) if match else fallback


def drawing_type_from_spec(spec: str) -> str:
    if "流程" in spec or "步骤" in spec:
        return "method_flow"
    if "数据流" in spec or "候选动作" in spec or "策略参数" in spec:
        return "data_flow"
    if "系统" in spec or "模块" in spec or "结构" in spec:
        return "system_block"
    return "method_flow"


def canvas_length(content_length: int) -> int:
    return max(content_length, math.ceil(content_length / CONTENT_RATIO_TARGET))


def centered_offset(canvas_length_value: int, content_length: int) -> int:
    return max(0, (canvas_length_value - content_length) // 2)


def validate_canvas(width: int, height: int, bbox: tuple[int, int, int, int]) -> dict[str, Any]:
    left, top, right, bottom = bbox
    content_width = max(0, right - left)
    content_height = max(0, bottom - top)
    width_ratio = content_width / width if width else 0
    height_ratio = content_height / height if height else 0
    return {
        "canvas_width": width,
        "canvas_height": height,
        "content_width_ratio": round(width_ratio, 3),
        "content_height_ratio": round(height_ratio, 3),
        "max_margin_ratio": round(
            max(
                left / width if width else 1,
                (width - right) / width if width else 1,
                top / height if height else 1,
                (height - bottom) / height if height else 1,
            ),
            3,
        ),
        "internal_title": False,
        "passes": width_ratio >= CONTENT_RATIO_FLOOR and height_ratio >= CONTENT_RATIO_FLOOR,
    }


def normalize_source_figures(data: dict[str, Any]) -> dict[int, dict[str, Any]]:
    raw_items = data.get("source_figures")
    if not isinstance(raw_items, list):
        return {}
    normalized: dict[int, dict[str, Any]] = {}
    for index, item in enumerate(raw_items, start=1):
        if not isinstance(item, dict):
            continue
        figure_no = int(item.get("figure_no") or index)
        normalized[figure_no] = {
            "figure_no": figure_no,
            "source_label": str(item.get("source_label") or f"Figure {figure_no}").strip(),
            "caption": str(item.get("caption") or "").strip(),
            "page": item.get("page"),
            "notes": str(item.get("notes") or "").strip(),
        }
    return normalized


def source_generation_text(source_figure: dict[str, Any] | None) -> str:
    if not isinstance(source_figure, dict):
        return ""
    parts: list[str] = []
    if source_figure.get("notes"):
        parts.extend(as_paragraphs(source_figure["notes"]))
    if source_figure.get("caption"):
        parts.extend(as_paragraphs(source_figure["caption"]))
    return "\n".join(parts).strip()


def merge_source_figure(asset: dict[str, Any], source_map: dict[int, dict[str, Any]]) -> dict[str, Any]:
    merged = dict(asset)
    figure_no = int(merged.get("figure_no") or 0)
    source_figure = source_map.get(figure_no)
    if source_figure:
        merged["source_figure"] = source_figure
    return merged


def generation_spec(asset: dict[str, Any]) -> str:
    parts: list[str] = []
    source_text = source_generation_text(asset.get("source_figure"))
    if source_text:
        parts.extend(as_paragraphs(source_text))
    spec = str(asset.get("spec") or "").strip()
    if spec:
        parts.extend(as_paragraphs(spec))
    deduped = list(dict.fromkeys(parts))
    return "\n".join(deduped).strip()


def infer_assets(data: dict[str, Any], output_dir: Path, prefix: str) -> list[dict[str, Any]]:
    source_map = normalize_source_figures(data)
    existing = data.get("drawing_assets")
    if isinstance(existing, list) and existing:
        return [
            merge_source_figure(asset, source_map)
            for asset in existing
            if isinstance(asset, dict)
        ]

    raw_drawings = data.get("drawings")
    if isinstance(raw_drawings, list):
        drawings = [str(item).strip() for item in raw_drawings if str(item).strip()]
    else:
        drawings = as_paragraphs(raw_drawings)
    assets: list[dict[str, Any]] = []
    for index, spec in enumerate(drawings, start=1):
        figure_no = figure_no_from_spec(spec, index)
        title = title_from_spec(spec, figure_no)
        svg_name = f"{prefix}_图{figure_no}.svg"
        assets.append(
            {
                "figure_no": figure_no,
                "title": title,
                "type": drawing_type_from_spec(spec),
                "spec": spec,
                "svg_path": str(output_dir / svg_name),
                "caption": f"图{figure_no} {title}",
                "abstract_candidate": figure_no == 1,
            }
        )
    if not assets and source_map:
        for figure_no, source_figure in sorted(source_map.items()):
            source_text = source_generation_text(source_figure)
            if not source_text:
                continue
            title = title_from_spec(source_text, figure_no)
            assets.append(
                {
                    "figure_no": figure_no,
                    "title": title,
                    "type": drawing_type_from_spec(source_text),
                    "spec": source_text,
                    "svg_path": str(output_dir / f"{prefix}_图{figure_no}.svg"),
                    "caption": f"图{figure_no} {title}",
                    "abstract_candidate": figure_no == 1,
                }
            )
    return [merge_source_figure(asset, source_map) for asset in assets]


def svg_header(width: int, height: int) -> list[str]:
    return [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#000000"/></marker></defs>',
        '<rect x="0" y="0" width="100%" height="100%" fill="#ffffff"/>',
    ]


def svg_footer() -> str:
    return "</svg>\n"


def text_block(
    x: int,
    y: int,
    label: str,
    anchor: str = "middle",
    size: int = 24,
    max_width: float = 260.0,
) -> str:
    lines = wrap_label(label, max_width, font_size=size)
    line_height = int(size * 1.25)
    start_y = y - (len(lines) - 1) * line_height // 2
    tspans = []
    for index, line in enumerate(lines):
        dy = 0 if index == 0 else line_height
        tspans.append(f'<tspan x="{x}" dy="{dy}">{xml(line)}</tspan>')
    return (
        f'<text x="{x}" y="{start_y}" text-anchor="{anchor}" '
        f'font-family="SimSun, Songti SC, serif" font-size="{size}" fill="#000000">'
        f'{"".join(tspans)}</text>'
    )


def rect_node(
    x: int, y: int, width: int, height: int, label: str, max_width: float | None = None
) -> str:
    """一个方框。`max_width` 缺省按盒宽减去左右内边距算，保证字不压框线。"""
    if max_width is None:
        max_width = inner_width(width)
    return (
        f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="0" ry="0" '
        'fill="#ffffff" stroke="#000000" stroke-width="2"/>'
        f"{text_block(x + width // 2, y + height // 2 + 8, label, max_width=max_width)}"
    )


def arrow(x1: int, y1: int, x2: int, y2: int) -> str:
    return (
        f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
        'stroke="#000000" stroke-width="2" marker-end="url(#arrow)"/>'
    )


def polyline_arrow(points: list[tuple[int, int]]) -> str:
    point_text = " ".join(f"{x},{y}" for x, y in points)
    return (
        f'<polyline points="{point_text}" fill="none" stroke="#000000" '
        'stroke-width="2" marker-end="url(#arrow)"/>'
    )


def box_arrow_points(
    first: tuple[int, int],
    second: tuple[int, int],
    node_w: int,
    node_h: int,
) -> tuple[int, int, int, int]:
    x1, y1 = first
    x2, y2 = second
    dx = x2 - x1
    dy = y2 - y1
    if abs(dx) >= abs(dy):
        direction = 1 if dx >= 0 else -1
        return (
            x1 + direction * (node_w // 2 + 6),
            y1,
            x2 - direction * (node_w // 2 + 8),
            y2,
        )
    direction = 1 if dy >= 0 else -1
    return (
        x1,
        y1 + direction * (node_h // 2 + 6),
        x2,
        y2 - direction * (node_h // 2 + 8),
    )


def orthogonal_points(
    first: tuple[int, int],
    second: tuple[int, int],
    node_h: int,
) -> list[tuple[int, int]]:
    x1, y1 = first
    x2, y2 = second
    direction = 1 if y2 >= y1 else -1
    start = (x1, y1 + direction * (node_h // 2 + 6))
    end = (x2, y2 - direction * (node_h // 2 + 8))
    mid_y = (start[1] + end[1]) // 2
    return [start, (x1, mid_y), (x2, mid_y), end]


#: 流程图方框里一条步骤最多留多少字。
#: 取值依据是排版而不是美观：盒宽 12 字 × 5 行 = 60 字，减去「S101 」这类前缀。
STEP_LABEL_LIMIT = 54


def extract_steps(spec: str) -> list[str]:
    """从规格里抽出「S101 ……」这样的步骤标签。

    这里**不做短截断**。早先是 `compact_label(label, 20)`，20 字放不下一句完整的
    技术步骤，于是图上每个方框都以「…」收尾、句子断在半途。附图是要交到审查员
    手里的东西，标签断句等于这幅图不能用。

    真正该短的是模型写进规格的那句话，不是脚本的剪刀。所以这里只留一个
    远高于正常长度的上限当跑飞保护，真触发了会在 stderr 上说一声。
    """
    steps = []
    for step_no, label in STEP_RE.findall(spec):
        cleaned = compact_label(label, STEP_LABEL_LIMIT)
        if cleaned.endswith("…"):
            print(
                f"步骤 {step_no} 的描述超过 {STEP_LABEL_LIMIT} 字，已在图上截断；"
                "建议把该步骤在附图规格里写得更概括。",
                file=sys.stderr,
            )
        steps.append(f"{step_no} {cleaned}")
    return steps


#: 结构框图里一个模块名最多留多少字。盒宽 430px 能排 3 行 × 约 15 字。
MODULE_LABEL_LIMIT = 40

#: 「这不是模块，是在描述图本身」的判据。
#:
#: 早先这里写的是 `"图" not in part` —— 只要模块名里带一个「图」字就整条丢掉。
#: 医学影像类专利的模块几乎清一色叫「图像采集模块」「图像预处理模块」，
#: 于是结构框图里**整块能力凭空消失**，而且不报错、图还画得好好的。
#: 现在只排除真正在讲图的短语（图中/如图/图示/箭头表示…）。
_NOT_A_MODULE = re.compile(r"^(图\s*\d*\s*[中示里内]|如图|见图|箭头|虚线|实线|参见)")


def extract_modules(spec: str) -> list[str]:
    match = re.search(r"包含(.+?)(?:。|\.|$)", spec)
    source = match.group(1) if match else spec
    source = source.replace("和", "、")
    modules: list[str] = []
    for raw in re.split(r"[、,，；;]", source):
        part = compact_label(raw, MODULE_LABEL_LIMIT)
        if not part or _NOT_A_MODULE.search(part) or "箭头" in part:
            continue
        modules.append(part)
    return modules[:8]


def require_items(items: list[str], asset: dict[str, Any], kind: str, minimum: int = 2) -> None:
    if len(items) < minimum:
        figure_no = asset.get("figure_no", "?")
        raise ValueError(
            f"Cannot generate figure {figure_no}: {kind} are not explicit enough in the drawing specification."
        )


#: 方框内文字的排版常量（与 text_block 的 size/line_height 对齐）。
FLOW_FONT_SIZE = 24
FLOW_LINE_HEIGHT = int(FLOW_FONT_SIZE * 1.25)
#: 方框内上下留白（两侧各一半）。
FLOW_PADDING_Y = 34


#: 方框左右内边距（两侧各一半），免得字贴着框线。
NODE_PADDING_X = 48


def inner_width(node_w: int) -> float:
    """方框内可用于排字的宽度。"""
    return float(max(FLOW_FONT_SIZE, node_w - NODE_PADDING_X))


def method_flow_layout(steps: list[str]) -> dict[str, Any]:
    node_w = 560
    max_width = inner_width(node_w)
    # 方框高度按**实际排几行**算，而不是写死。
    # 写死 82px 的后果是：文字多了就往框外溢或者被上游截掉，
    # 两种表现都指向同一个观感——「图是残的」。
    line_count = max(
        (len(wrap_label(step, max_width, font_size=FLOW_FONT_SIZE)) for step in steps),
        default=1,
    )
    node_h = max(82, line_count * FLOW_LINE_HEIGHT + FLOW_PADDING_Y)
    gap = 48
    content_w = node_w
    content_h = len(steps) * node_h + max(0, len(steps) - 1) * gap
    width = canvas_length(content_w)
    height = canvas_length(content_h)
    x = centered_offset(width, content_w)
    top = centered_offset(height, content_h)
    positions = [(x, top + index * (node_h + gap)) for index in range(len(steps))]
    return {
        "width": width,
        "height": height,
        "node_w": node_w,
        "node_h": node_h,
        "gap": gap,
        "max_width": max_width,
        "positions": positions,
        "bbox": (x, top, x + content_w, top + content_h),
    }


def vertical_block_layout(items: list[str], font_size: int = 23) -> dict[str, Any]:
    node_w = 430
    max_width = inner_width(node_w)
    # 与流程图同一条规矩：盒子按真正排出来的行数长高，不写死
    line_count = max(
        (len(wrap_label(item, max_width, font_size=font_size)) for item in items), default=1
    )
    node_h = max(72, line_count * int(font_size * 1.25) + FLOW_PADDING_Y)
    gap = 52
    content_w = node_w
    content_h = len(items) * node_h + max(0, len(items) - 1) * gap
    width = canvas_length(content_w)
    height = canvas_length(content_h)
    x = centered_offset(width, content_w)
    top = centered_offset(height, content_h)
    positions = [(x, top + index * (node_h + gap)) for index in range(len(items))]
    return {
        "width": width,
        "height": height,
        "node_w": node_w,
        "node_h": node_h,
        "max_width": max_width,
        "positions": positions,
        "bbox": (x, top, x + content_w, top + content_h),
    }


def data_flow_layout(nodes: list[str], font_size: int = 22) -> dict[str, Any]:
    node_w = 190
    max_width = inner_width(node_w)
    line_count = max(
        (len(wrap_label(node, max_width, font_size=font_size)) for node in nodes), default=1
    )
    node_h = max(78, line_count * int(font_size * 1.25) + FLOW_PADDING_Y)
    gap_x = 54
    gap_y = 64
    rows = 1 if len(nodes) <= 3 else 2
    cols = math.ceil(len(nodes) / rows)
    content_w = cols * node_w + max(0, cols - 1) * gap_x
    content_h = rows * node_h + max(0, rows - 1) * gap_y
    width = canvas_length(content_w)
    height = canvas_length(content_h)
    left = centered_offset(width, content_w)
    top = centered_offset(height, content_h)
    positions = []
    for index in range(len(nodes)):
        row = index // cols
        col = index % cols
        if row % 2 == 1:
            col = cols - 1 - col
        x = left + col * (node_w + gap_x)
        y = top + row * (node_h + gap_y)
        positions.append((x, y))
    return {
        "width": width,
        "height": height,
        "node_w": node_w,
        "node_h": node_h,
        "max_width": max_width,
        "positions": positions,
        "bbox": (left, top, left + content_w, top + content_h),
    }


def node_index(nodes: list[str], keyword: str) -> int | None:
    for index, label in enumerate(nodes):
        if keyword in label:
            return index
    return None


def data_flow_edges(nodes: list[str]) -> list[tuple[int, int]]:
    return [(index, index + 1) for index in range(len(nodes) - 1)]


def data_flow_extra_edges(spec: str, nodes: list[str]) -> list[tuple[int, int]]:
    edges: list[tuple[int, int]] = []
    base_edges = set(data_flow_edges(nodes))

    def add_edge(source: int | None, target: int | None) -> None:
        if source is None or target is None or source == target:
            return
        edge = (source, target)
        if edge not in base_edges and edge not in edges:
            edges.append(edge)

    if "共同输入" in spec and "冻结价值环境模型" in spec:
        add_edge(node_index(nodes, "离线状态"), node_index(nodes, "冻结价值环境模型"))
        add_edge(node_index(nodes, "候选动作"), node_index(nodes, "冻结价值环境模型"))
    if "反向作用于策略模型" in spec or "反向作用于所述策略模型" in spec:
        add_edge(node_index(nodes, "策略参数更新"), node_index(nodes, "策略模型"))
    return edges


def method_flow_svg(asset: dict[str, Any]) -> str:
    steps = extract_steps(generation_spec(asset))
    require_items(steps, asset, "method steps")
    layout = method_flow_layout(steps)
    width = layout["width"]
    height = layout["height"]
    node_w = layout["node_w"]
    node_h = layout["node_h"]
    gap = layout["gap"]
    asset["validation"] = validate_canvas(width, height, layout["bbox"])
    parts = svg_header(width, height)
    # 换行口径必须与算高时用的是同一个，否则算出来的盒子高度对不上真正排出来的行数
    max_width = layout["max_width"]
    for index, (label, (x, y)) in enumerate(zip(steps, layout["positions"])):
        parts.append(rect_node(x, y, node_w, node_h, label, max_width=max_width))
        if index < len(steps) - 1:
            parts.append(arrow(width // 2, y + node_h + 6, width // 2, y + node_h + gap - 8))
    parts.append(svg_footer())
    return "\n".join(parts)


def system_block_svg(asset: dict[str, Any]) -> str:
    modules = extract_modules(generation_spec(asset))
    require_items(modules, asset, "system modules", minimum=2)
    layout = vertical_block_layout(modules)
    width = layout["width"]
    height = layout["height"]
    node_w = layout["node_w"]
    node_h = layout["node_h"]
    asset["validation"] = validate_canvas(width, height, layout["bbox"])
    parts = svg_header(width, height)
    centers = []
    for (x, y), label in zip(layout["positions"], modules):
        parts.append(rect_node(x, y, node_w, node_h, label, max_width=layout["max_width"]))
        centers.append((x + node_w // 2, y + node_h // 2))
    for index in range(len(centers) - 1):
        x1, y1 = centers[index]
        x2, y2 = centers[index + 1]
        parts.append(arrow(x1, y1 + node_h // 2 + 6, x2, y2 - node_h // 2 - 8))
    parts.append(svg_footer())
    return "\n".join(parts)


def data_flow_svg(asset: dict[str, Any]) -> str:
    spec = generation_spec(asset)
    nodes = extract_modules(spec)
    require_items(nodes, asset, "data-flow nodes", minimum=2)
    layout = data_flow_layout(nodes)
    width = layout["width"]
    height = layout["height"]
    node_w = layout["node_w"]
    node_h = layout["node_h"]
    asset["validation"] = validate_canvas(width, height, layout["bbox"])
    parts = svg_header(width, height)
    centers: list[tuple[int, int]] = []
    for label, (x, y) in zip(nodes, layout["positions"]):
        parts.append(rect_node(x, y, node_w, node_h, label))
        centers.append((x + node_w // 2, y + node_h // 2))
    for source, target in data_flow_edges(nodes):
        parts.append(arrow(*box_arrow_points(centers[source], centers[target], node_w, node_h)))
    for source, target in data_flow_extra_edges(spec, nodes):
        parts.append(polyline_arrow(orthogonal_points(centers[source], centers[target], node_h)))
    parts.append(svg_footer())
    return "\n".join(parts)


def render_svg(asset: dict[str, Any]) -> str:
    drawing_type = str(asset.get("type") or "")
    if drawing_type == "system_block":
        return system_block_svg(asset)
    if drawing_type == "data_flow":
        return data_flow_svg(asset)
    return method_flow_svg(asset)


def load_font(size: int):
    try:
        from PIL import ImageFont
    except ImportError as exc:  # pragma: no cover - depends on local environment
        raise RuntimeError("Pillow is required for PNG fallback generation.") from exc

    candidates = [
        r"C:\Windows\Fonts\simsun.ttc",
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\arial.ttf",
    ]
    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def draw_center_text(
    draw: Any, x: int, y: int, label: str, font: Any, max_width: float = 260.0,
    font_size: int = FLOW_FONT_SIZE,
) -> None:
    # 折行用与 SVG / 布局完全相同的估算，保证三者行数一致；
    # 具体像素宽仍由 PIL 量，只用于水平居中
    lines = wrap_label(label, max_width, font_size=font_size)
    metrics = []
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        metrics.append((line, bbox[2] - bbox[0], bbox[3] - bbox[1]))
    line_height = max((height for _, _, height in metrics), default=24) + 8
    top = y - (line_height * len(metrics)) // 2
    for index, (line, width, _) in enumerate(metrics):
        draw.text((x - width // 2, top + index * line_height), line, fill="black", font=font)


def draw_rect_node(
    draw: Any,
    x: int,
    y: int,
    width: int,
    height: int,
    label: str,
    font: Any,
    max_width: float | None = None,
    font_size: int = FLOW_FONT_SIZE,
) -> None:
    if max_width is None:
        max_width = inner_width(width)
    draw.rectangle((x, y, x + width, y + height), outline="black", width=2, fill="white")
    draw_center_text(
        draw, x + width // 2, y + height // 2, label, font,
        max_width=max_width, font_size=font_size,
    )


def draw_arrow(draw: Any, x1: int, y1: int, x2: int, y2: int) -> None:
    draw.line((x1, y1, x2, y2), fill="black", width=2)
    angle = math.atan2(y2 - y1, x2 - x1)
    length = 16
    spread = 0.45
    p1 = (
        x2 - length * math.cos(angle - spread),
        y2 - length * math.sin(angle - spread),
    )
    p2 = (
        x2 - length * math.cos(angle + spread),
        y2 - length * math.sin(angle + spread),
    )
    draw.polygon([(x2, y2), p1, p2], fill="black")


def draw_polyline_arrow(draw: Any, points: list[tuple[int, int]]) -> None:
    for first, second in zip(points, points[1:]):
        draw.line((*first, *second), fill="black", width=2)
    if len(points) >= 2:
        x1, y1 = points[-2]
        x2, y2 = points[-1]
        angle = math.atan2(y2 - y1, x2 - x1)
        length = 16
        spread = 0.45
        p1 = (
            x2 - length * math.cos(angle - spread),
            y2 - length * math.sin(angle - spread),
        )
        p2 = (
            x2 - length * math.cos(angle + spread),
            y2 - length * math.sin(angle + spread),
        )
        draw.polygon([(x2, y2), p1, p2], fill="black")


def png_canvas(width: int, height: int):
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:  # pragma: no cover - depends on local environment
        raise RuntimeError("Pillow is required for PNG fallback generation.") from exc

    image = Image.new("RGB", (width, height), "white")
    return image, ImageDraw.Draw(image)


def method_flow_png(asset: dict[str, Any], output: Path) -> None:
    steps = extract_steps(generation_spec(asset))
    require_items(steps, asset, "method steps")
    layout = method_flow_layout(steps)
    width = layout["width"]
    height = layout["height"]
    node_w = layout["node_w"]
    node_h = layout["node_h"]
    gap = layout["gap"]
    image, draw = png_canvas(width, height)
    body_font = load_font(FLOW_FONT_SIZE)
    max_width = layout["max_width"]
    for index, (label, (x, y)) in enumerate(zip(steps, layout["positions"])):
        draw_rect_node(draw, x, y, node_w, node_h, label, body_font, max_width=max_width)
        if index < len(steps) - 1:
            draw_arrow(draw, width // 2, y + node_h + 6, width // 2, y + node_h + gap - 8)
    image.save(output)


def system_block_png(asset: dict[str, Any], output: Path) -> None:
    modules = extract_modules(generation_spec(asset))
    require_items(modules, asset, "system modules", minimum=2)
    layout = vertical_block_layout(modules)
    width = layout["width"]
    height = layout["height"]
    node_w = layout["node_w"]
    node_h = layout["node_h"]
    image, draw = png_canvas(width, height)
    body_font = load_font(23)
    centers = []
    for (x, y), label in zip(layout["positions"], modules):
        draw_rect_node(
            draw, x, y, node_w, node_h, label, body_font,
            max_width=layout["max_width"], font_size=23,
        )
        centers.append((x + node_w // 2, y + node_h // 2))
    for index in range(len(centers) - 1):
        x1, y1 = centers[index]
        x2, y2 = centers[index + 1]
        draw_arrow(draw, x1, y1 + node_h // 2 + 6, x2, y2 - node_h // 2 - 8)
    image.save(output)


def data_flow_png(asset: dict[str, Any], output: Path) -> None:
    spec = generation_spec(asset)
    nodes = extract_modules(spec)
    require_items(nodes, asset, "data-flow nodes", minimum=2)
    layout = data_flow_layout(nodes)
    width = layout["width"]
    height = layout["height"]
    node_w = layout["node_w"]
    node_h = layout["node_h"]
    image, draw = png_canvas(width, height)
    body_font = load_font(22)
    centers: list[tuple[int, int]] = []
    for label, (x, y) in zip(nodes, layout["positions"]):
        draw_rect_node(
            draw, x, y, node_w, node_h, label, body_font,
            max_width=layout["max_width"], font_size=22,
        )
        centers.append((x + node_w // 2, y + node_h // 2))
    for source, target in data_flow_edges(nodes):
        draw_arrow(draw, *box_arrow_points(centers[source], centers[target], node_w, node_h))
    for source, target in data_flow_extra_edges(spec, nodes):
        draw_polyline_arrow(draw, orthogonal_points(centers[source], centers[target], node_h))
    image.save(output)


def render_png(asset: dict[str, Any], output: Path) -> None:
    drawing_type = str(asset.get("type") or "")
    if drawing_type == "system_block":
        system_block_png(asset, output)
    elif drawing_type == "data_flow":
        data_flow_png(asset, output)
    else:
        method_flow_png(asset, output)


def stored_path(path: Path, base_dir: Path) -> str:
    try:
        return str(path.relative_to(base_dir.resolve()))
    except ValueError:
        return str(path)


def image_model_prompt(asset: dict[str, Any]) -> str:
    figure_no = int(asset.get("figure_no") or 0)
    spec = generation_spec(asset) or str(asset.get("caption") or "").strip()
    validation = asset.get("validation") if isinstance(asset.get("validation"), dict) else {}
    width_ratio = validation.get("content_width_ratio", "约0.80")
    height_ratio = validation.get("content_height_ratio", "约0.80")
    source_figure = asset.get("source_figure") if isinstance(asset.get("source_figure"), dict) else {}
    source_bits = []
    if source_figure.get("source_label"):
        source_bits.append(str(source_figure["source_label"]))
    if source_figure.get("page") not in (None, ""):
        source_bits.append(f"page {source_figure['page']}")
    if source_figure.get("caption"):
        source_bits.append(f"caption: {source_figure['caption']}")
    source_context = f"原始参考来源：{'；'.join(source_bits)}。\n" if source_bits else ""
    return (
        f"请生成中国发明专利申请的说明书附图图{figure_no}，严格依据参考附图结构和以下附图规格重绘：{spec}\n"
        f"{source_context}"
        "硬性要求：纯黑白线条图，纯白背景；不得使用彩色、灰度、渐变、阴影、照片、三维渲染、装饰元素、logo或水印；"
        "图片内部不得出现图号、附图标题、说明性段落或与技术方案无关的文字；"
        "仅保留说明书和权利要求支持的步骤编号、模块名称、关键术语、标号和箭头；"
        f"主体内容应填充画布，目标内容宽度占比不低于{width_ratio}、高度占比不低于{height_ratio}，不得留下大面积空白；"
        "不得新增、删除或改变任何模块、步骤、连接关系和流程方向。"
    )


def generate(
    data: dict[str, Any],
    input_path: Path,
    output_dir: Path,
    prefix: str,
    png_fallback: bool = True,
) -> list[dict[str, Any]]:
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    assets = infer_assets(data, output_dir, prefix)
    normalized: list[dict[str, Any]] = []
    for asset in sorted(assets, key=lambda item: int(item.get("figure_no") or 0)):
        figure_no = int(asset.get("figure_no") or len(normalized) + 1)
        asset["figure_no"] = figure_no
        preferred_title = title_from_spec(generation_spec(asset) or str(asset.get("spec") or ""), figure_no)
        current_title = str(asset.get("title") or "").strip()
        if (
            not current_title
            or current_title == f"图{figure_no}"
            or (not has_cjk(current_title) and has_cjk(preferred_title))
        ):
            asset["title"] = preferred_title
        else:
            asset["title"] = current_title
        current_caption = str(asset.get("caption") or "").strip()
        preferred_caption = f"图{figure_no} {asset['title']}"
        if (
            not current_caption
            or current_caption == f"图{figure_no} {current_title}"
            or (not has_cjk(current_caption) and has_cjk(asset["title"]))
        ):
            asset["caption"] = preferred_caption
        else:
            asset["caption"] = current_caption
        asset.setdefault("abstract_candidate", figure_no == 1)
        if not asset.get("svg_path"):
            asset["svg_path"] = str(output_dir / f"{prefix}_图{figure_no}.svg")
        svg_path = Path(str(asset["svg_path"]))
        if not svg_path.is_absolute():
            svg_path = (input_path.parent / svg_path).resolve()
        svg_path.parent.mkdir(parents=True, exist_ok=True)
        svg_path.write_text(render_svg(asset), encoding="utf-8")
        asset["svg_path"] = stored_path(svg_path, input_path.parent)
        if png_fallback:
            png_path = svg_path.with_suffix(".png")
            try:
                render_png(asset, png_path)
                asset["png_path"] = stored_path(png_path, input_path.parent)
            except RuntimeError:
                asset.pop("png_path", None)
        asset["image_model_prompt"] = image_model_prompt(asset)
        normalized.append(asset)
    data["drawing_assets"] = normalized
    data["image_model_prompts"] = [
        {"figure_no": asset["figure_no"], "prompt": asset["image_model_prompt"]}
        for asset in normalized
    ]
    data["drawing_validation"] = [
        {"figure_no": asset["figure_no"], **asset.get("validation", {})}
        for asset in normalized
    ]
    return normalized


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate patent drawing SVG files from JSON.")
    parser.add_argument("input", type=Path, help="Path to patent content JSON.")
    parser.add_argument("-o", "--output-dir", type=Path, help="Directory for SVG files.")
    parser.add_argument("--prefix", help="Output filename prefix.")
    parser.add_argument(
        "--no-png-fallback",
        action="store_true",
        help="Do not generate PNG fallback files for DOCX/PDF converters.",
    )
    parser.add_argument(
        "--update-json",
        action="store_true",
        help="Write generated drawing_assets metadata back to the input JSON.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    data = read_json(args.input)
    output_dir = args.output_dir or args.input.parent
    prefix = args.prefix or default_prefix(args.input)
    assets = generate(data, args.input, output_dir, prefix, png_fallback=not args.no_png_fallback)
    if args.update_json:
        write_json(args.input, data)
    for asset in assets:
        print(f"Wrote {asset['svg_path']}")
        if asset.get("png_path"):
            print(f"Wrote {asset['png_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
