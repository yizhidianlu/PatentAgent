# -*- coding: utf-8 -*-
"""机械结构线稿：剖面线必须是真的 45°，且不得跑出剖切区。

审查指南对说明书附图的要求里，剖面线 45° 是硬指标。此前的实现有两处出入，
都是肉眼可见的：

1. 端点只把 x 夹进区间、y 恒取上下边 —— 被「裁」过的线角度被改掉，
   多根线收敛到同一个角点，渲染出来是一把戳出方块的扇形；
2. 水平偏移是个常量，于是实际角度随剖切区高度变化（区域高 26 时是 81.3°，
   不是 45°），只有高度恰好等于那个常量时才对。

所以这里不验「画了几根线」，验的是角度与包含关系——那才是规范本身。
"""
from __future__ import annotations

import math

import pytest

ezdxf = pytest.importorskip("ezdxf", reason="未安装 ezdxf，跳过线稿测试")

from app.tools import cad_lineart  # noqa: E402


def _hatch_lines(x0: float, y0: float, x1: float, y1: float) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    doc = ezdxf.new("R2010", setup=True)
    msp = doc.modelspace()
    cad_lineart._add_hatch(msp, x0, y0, x1, y1)
    return [
        ((e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y))
        for e in msp.query("LINE")
    ]


@pytest.mark.parametrize(
    "box",
    [
        (22.0, 22.0, 48.0, 48.0),   # 正方形：老实现在这里画出扇形
        (0.0, 0.0, 100.0, 10.0),    # 扁：老实现角度会变平
        (5.0, 5.0, 15.0, 90.0),     # 高：老实现角度会变竖
    ],
)
def test_every_hatch_line_is_45_degrees(box) -> None:
    x0, y0, x1, y1 = box
    lines = _hatch_lines(x0, y0, x1, y1)
    assert lines, "剖切区里必须有剖面线"
    for (ax, ay), (bx, by) in lines:
        angle = math.degrees(math.atan2(by - ay, bx - ax))
        assert angle == pytest.approx(45.0, abs=1e-6), f"{(ax, ay)}→{(bx, by)} 不是 45°"


@pytest.mark.parametrize(
    "box",
    [
        (22.0, 22.0, 48.0, 48.0),
        (0.0, 0.0, 100.0, 10.0),
        (5.0, 5.0, 15.0, 90.0),
    ],
)
def test_no_hatch_line_leaves_the_region(box) -> None:
    x0, y0, x1, y1 = box
    eps = 1e-9
    for (ax, ay), (bx, by) in _hatch_lines(x0, y0, x1, y1):
        for px, py in ((ax, ay), (bx, by)):
            assert x0 - eps <= px <= x1 + eps, f"x={px} 跑出 [{x0}, {x1}]"
            assert y0 - eps <= py <= y1 + eps, f"y={py} 跑出 [{y0}, {y1}]"


def test_hatch_covers_the_region() -> None:
    """裁剪不能把覆盖裁没了：区域中线上应当被多根剖面线穿过。"""
    x0, y0, x1, y1 = 22.0, 22.0, 48.0, 48.0
    mid_y = (y0 + y1) / 2
    crossings = []
    for (ax, ay), (bx, by) in _hatch_lines(x0, y0, x1, y1):
        if min(ay, by) <= mid_y <= max(ay, by):
            t = (mid_y - ay) / (by - ay) if by != ay else 0.0
            crossings.append(ax + t * (bx - ax))
    assert len(crossings) >= 4, f"中线只被 {len(crossings)} 根线穿过，覆盖不足"
    crossings.sort()
    gaps = [b - a for a, b in zip(crossings, crossings[1:])]
    assert max(gaps) <= cad_lineart.HATCH_STEP + 1e-6, "相邻剖面线间距超过设定值"


def test_degenerate_region_draws_nothing() -> None:
    """零高/零宽的剖切区不该画出任何东西，更不该抛异常。"""
    assert _hatch_lines(10.0, 10.0, 10.0, 20.0) == []
    assert _hatch_lines(10.0, 10.0, 20.0, 10.0) == []
    assert _hatch_lines(20.0, 20.0, 10.0, 10.0) == []
