# -*- coding: utf-8 -*-
"""说明书附图的标签不得被阉割。

用户反馈：生成的附图「显示不完整以及文字被阉割」。查下来是三处独立的截断叠在一起：

1. `extract_steps` 把每条步骤硬截到 20 字并接一个「…」——句子断在半途；
2. `wrap_label` 折行后 `return lines[:3]`，多出来的行**直接丢掉，一声不吭**；
3. 布局算盒高与渲染画字用的是两个不同的 `max_chars`，算出来的盒子装不下真正
   排出来的行数，字压到框线外面。

还有一处更严重的：`extract_modules` 用 `"图" not in part` 过滤，
于是「喉镜图像采集模块」这类名字里带「图」的模块**整条从结构框图里消失**。
医学影像类专利的模块几乎清一色带「图像」二字，这一条会让整块能力凭空不见，
而且不报错、图还画得好好的。

附图是要交到审查员手里的东西，所以这些都不是观感问题。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "app" / "tools" / "generate_patent_drawings.py"
_spec = importlib.util.spec_from_file_location("_gpd", _MODULE_PATH)
assert _spec and _spec.loader
gpd = importlib.util.module_from_spec(_spec)
sys.modules["_gpd"] = gpd
_spec.loader.exec_module(gpd)


STEP_SPEC = (
    "图1为本发明方法的流程示意图；"
    "S101，获取视频喉镜在气管插管场景中采集的喉镜图像帧，作为待检测图像；"
    "S102，将待检测图像输入经MobileNetV4轻量化卷积块重建的骨干网络提取多尺度特征图；"
    "S103，将骨干网络末端的高层特征依次经SPPF与BiFPN颈部进行双向多尺度融合。"
)

MODULE_SPEC = (
    "图1为本发明系统的结构框图，包含喉镜图像采集模块、MobileNetV4轻量化骨干网络模块、"
    "BiFPN双向多尺度融合颈部模块、DAT可变形注意力精炼模块、多尺度检测头模块。"
    "图中箭头表示数据流向。"
)


# ---------------------------------------------------------------------------
# 步骤标签
# ---------------------------------------------------------------------------


def test_step_labels_are_not_cut_mid_sentence() -> None:
    """完整的步骤描述必须原样进图，不许出现半截句子。"""
    steps = gpd.extract_steps(STEP_SPEC)
    assert len(steps) == 3
    assert steps[0].startswith("S101 ")
    assert "作为待检测图像" in steps[0], f"步骤被截断了：{steps[0]}"
    assert not any(s.endswith("…") for s in steps), steps


def test_model_names_are_never_split_across_lines() -> None:
    """`MobileNetV4` 被断成 `MobileNetV` / `4`，在专利附图里就是个错字。"""
    label = "S102 将待检测图像输入经MobileNetV4轻量化卷积块重建的骨干网络提取多尺度特征图"
    lines = gpd.wrap_label(label, gpd.inner_width(560), font_size=24)
    joined = "".join(lines)
    assert "MobileNetV4" in joined
    assert any("MobileNetV4" in line for line in lines), f"模型名被拆散了：{lines}"


def test_wrapped_lines_fit_inside_the_box() -> None:
    """每一行的估算宽度都必须落在盒内——否则字会压到框线上。"""
    max_width = gpd.inner_width(560)
    for step in gpd.extract_steps(STEP_SPEC):
        for line in gpd.wrap_label(step, max_width, font_size=24):
            assert gpd.text_width(line, 24) <= max_width, f"这一行超宽：{line}"


def test_box_height_matches_the_lines_actually_drawn() -> None:
    """盒高按真正排出来的行数算。

    这条是三处截断里最隐蔽的一处：布局与渲染各用各的折行口径，
    盒子看着有边框、字却溢出去——用户看到的就是「显示不完整」。
    """
    steps = gpd.extract_steps(STEP_SPEC)
    layout = gpd.method_flow_layout(steps)
    line_height = int(gpd.FLOW_FONT_SIZE * 1.25)
    for step in steps:
        lines = gpd.wrap_label(step, layout["max_width"], font_size=gpd.FLOW_FONT_SIZE)
        assert len(lines) * line_height <= layout["node_h"], (
            f"{len(lines)} 行装不进 {layout['node_h']}px 的盒子：{step}"
        )


def test_only_a_genuinely_runaway_label_gets_truncated() -> None:
    """兜底截断仍然要有，但阈值必须高到正常步骤碰不到。"""
    steps = gpd.extract_steps("S101，" + "极长的步骤描述" * 20 + "。")
    assert steps[0].endswith("…"), "跑飞的标签仍应被截断，否则会撑破画布"
    assert len(steps[0]) > 40, "兜底阈值不该低到正常步骤也被削"


# ---------------------------------------------------------------------------
# 结构框图的模块
# ---------------------------------------------------------------------------


def test_modules_containing_the_character_for_figure_are_kept() -> None:
    """「喉镜图像采集模块」不能因为带个「图」字就整条消失。"""
    modules = gpd.extract_modules(MODULE_SPEC)
    assert "喉镜图像采集模块" in modules, f"带「图」字的模块被丢了：{modules}"
    assert len(modules) == 5, modules


def test_sentences_about_the_figure_are_still_excluded() -> None:
    """放宽过滤不能把「图中箭头表示数据流向」这类描述也当成模块画进去。"""
    modules = gpd.extract_modules(MODULE_SPEC)
    assert not any("箭头" in m for m in modules), modules
    assert not any(m.startswith("图中") for m in modules), modules


def test_module_names_are_not_cut_to_twelve_characters() -> None:
    modules = gpd.extract_modules(MODULE_SPEC)
    assert "MobileNetV4轻量化骨干网络模块" in modules, modules
    assert not any(m.endswith("…") for m in modules), modules


# ---------------------------------------------------------------------------
# 端到端：真渲染一遍
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec,kind", [(STEP_SPEC, "method_flow"), (MODULE_SPEC, "system_block")])
def test_rendered_svg_contains_the_full_text(spec: str, kind: str) -> None:
    """SVG 里必须找得到完整词句，且一个「…」都不该有。"""
    asset = {"figure_no": 1, "spec": spec, "type": kind, "caption": "图1"}
    svg = gpd.render_svg(asset)
    assert "…" not in svg, "渲染结果里出现了省略号，说明还有地方在截断"
    if kind == "method_flow":
        assert "作为待检测图像" in svg
    else:
        assert "喉镜图像采集模块" in svg


def test_rendered_png_is_not_blank(tmp_path: Path) -> None:
    """顺带守住渲染本身：出一张全白图和出错一样糟，因为它不会报错。"""
    pytest.importorskip("PIL", reason="未安装 Pillow")
    asset = {"figure_no": 1, "spec": STEP_SPEC, "type": "method_flow", "caption": "图1"}
    out = tmp_path / "f.png"
    gpd.render_png(asset, out)
    assert out.is_file() and out.stat().st_size > 2000


# ---------------------------------------------------------------------------
# 自适应：盒子迁就内容，不是内容迁就盒子
# ---------------------------------------------------------------------------


def test_long_real_world_steps_are_not_truncated() -> None:
    """机主真实案子里的步骤长度（50+ 字），必须一个「…」都没有。

    此前把上限从 20 提到 54 只是把截断线往后挪；真正的修法是盒子按内容变宽。
    """
    spec = (
        "图1；S103，将多尺度特征图输入双向特征金字塔颈部，构建自顶向下通路与自底向上通路，"
        "将不同分辨率的特征对齐至同一尺度后按归一化可学习权重加权聚合；"
        "S104，在每个检测层前通过可变形注意力模块对多尺度融合特征图进行细化，"
        "由轻量偏移网络根据查询嵌入预测空间偏移以确定采样位置。"
    )
    steps = gpd.extract_steps(spec)
    assert len(steps) == 2
    assert not any(s.endswith("…") for s in steps), steps
    assert "加权聚合" in steps[0] and "确定采样位置" in steps[1]

    layout = gpd.method_flow_layout(steps)
    for step in steps:
        lines = gpd.wrap_label(step, layout["max_width"], font_size=gpd.FLOW_FONT_SIZE)
        assert len(lines) * int(gpd.FLOW_FONT_SIZE * 1.25) <= layout["node_h"]
        for line in lines:
            assert gpd.text_width(line, gpd.FLOW_FONT_SIZE) <= layout["max_width"]


def test_box_width_grows_with_content_within_bounds() -> None:
    """长内容加宽盒子，但不会宽到在 A4 上缩得看不清。"""
    short = gpd.method_flow_layout(["S101 采集图像", "S102 输出结果"])["node_w"]
    long_steps = gpd.extract_steps(
        "图1；S101，" + "以极长的技术描述说明本步骤的输入输出与判定条件" * 4 + "。"
    )
    wide = gpd.method_flow_layout(long_steps)["node_w"]
    assert wide > short, "长内容应当把盒子撑宽"
    assert gpd.NODE_W_MIN <= wide <= gpd.NODE_W_MAX
