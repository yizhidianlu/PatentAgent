# -*- coding: utf-8 -*-
"""机械结构线稿生成 —— 由结构描述 JSON 出符合专利附图规范的黑白线条图。

    python cad_lineart.py --spec spec.json --out-dir <目录> --stem figure_2

产物：`<stem>.png`（嵌文档用）、`<stem>.dxf`（矢量源，用户可拿去 CAD 里改）。

**为什么用 ezdxf 而不是 3D CAD。**
专利附图要的是「黑白线条工程图」，不是三维模型的渲染。CadQuery / build123d /
FreeCAD 那条路要先建实体再投影，重、慢、依赖大，而且投影出来还要再处理成
合规线条。ezdxf 直接画二维图元，产物天然就是线条，也天然带 DXF 矢量源——
专利代理人拿到 DXF 可以在自己的 CAD 里继续改，这比给一张位图有用得多。

**规范要点**（中国专利审查指南对附图的要求）：
- 纯黑白线条，白色背景；不得有颜色、灰度、渐变、阴影、照片、三维渲染
- 零件用阿拉伯数字标号 + 引出线标注，图内不写说明性文字
- 剖面用 45° 剖面线
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# 线宽（DXF lineweight，单位 0.01mm）。专利图讲究线条粗细分级：
# 轮廓粗、内部结构中、剖面线与引线细。
LW_OUTLINE = 35
LW_INNER = 20
LW_THIN = 9

# 剖面线间距（图元坐标单位，沿 X 方向量取）
HATCH_STEP = 5.0


def _add_polyline(msp, points: list[list[float]], *, closed: bool, weight: int) -> None:
    msp.add_lwpolyline(
        [(float(x), float(y)) for x, y in points],
        close=closed,
        dxfattribs={"lineweight": weight},
    )


def _add_hatch(msp, x0: float, y0: float, x1: float, y1: float) -> None:
    """45° 剖面线。用细实线画，不用 DXF HATCH 实体——

    HATCH 在部分渲染后端会被填充成灰色块，那在专利附图里是不允许的。

    **裁剪必须按参数 t 同时裁 x 和 y。**
    早先的写法只把端点的 x 夹进 [x0, x1]、y 恒取 y0/y1：这既改变了被裁那几根线的
    角度（多根线收敛到同一个角点，渲染出来是一把戳出方块的扇形），也让「45°」
    根本不成立——水平偏移曾是个常量，于是实际角度随剖切区高度变化，
    只有高度恰好等于那个常量时才是 45°。审查指南要求剖面线 45°，这条得是真的。
    """
    height = y1 - y0
    if height <= 0 or x1 <= x0:
        return
    # 45° ⇒ 水平偏移等于高度。起点从 x0 - height 开始，才能覆盖左下角那一片
    start = x0 - height
    count = int((x1 - start) // HATCH_STEP) + 2
    for i in range(count):
        sx = start + i * HATCH_STEP
        # 线段 (sx, y0) → (sx + height, y1) 按 t 裁到 x ∈ [x0, x1]
        t0 = max(0.0, (x0 - sx) / height)
        t1 = min(1.0, (x1 - sx) / height)
        if t1 <= t0:
            continue           # 整根落在剖切区外
        msp.add_line(
            (sx + t0 * height, y0 + t0 * height),
            (sx + t1 * height, y0 + t1 * height),
            dxfattribs={"lineweight": LW_THIN},
        )


def build(spec: dict[str, Any], out_dir: Path, stem: str) -> dict[str, str]:
    import ezdxf
    from ezdxf.addons.drawing.matplotlib import qsave

    doc = ezdxf.new("R2010", setup=True)
    msp = doc.modelspace()

    for shape in spec.get("shapes") or []:
        kind = str(shape.get("kind") or "").lower()
        weight = {"outline": LW_OUTLINE, "inner": LW_INNER, "thin": LW_THIN}.get(
            str(shape.get("weight") or "inner"), LW_INNER
        )
        if kind == "polyline":
            _add_polyline(msp, shape["points"], closed=bool(shape.get("closed", True)), weight=weight)
        elif kind == "rect":
            x, y, w, h = (float(shape[k]) for k in ("x", "y", "w", "h"))
            _add_polyline(msp, [[x, y], [x + w, y], [x + w, y + h], [x, y + h]],
                          closed=True, weight=weight)
        elif kind == "circle":
            msp.add_circle(
                (float(shape["cx"]), float(shape["cy"])),
                radius=float(shape["r"]),
                dxfattribs={"lineweight": weight},
            )
        elif kind == "line":
            msp.add_line(
                (float(shape["x1"]), float(shape["y1"])),
                (float(shape["x2"]), float(shape["y2"])),
                dxfattribs={"lineweight": weight},
            )
        elif kind == "hatch":
            _add_hatch(msp, float(shape["x0"]), float(shape["y0"]),
                       float(shape["x1"]), float(shape["y1"]))
        else:
            print(f"跳过未知图元: {kind}", file=sys.stderr)

    # 标号 + 引出线。专利附图只放数字，不放说明文字——说明写在说明书里。
    for c in spec.get("callouts") or []:
        label = str(c.get("label", "")).strip()
        tx, ty = float(c["text_x"]), float(c["text_y"])
        ax, ay = float(c["arrow_x"]), float(c["arrow_y"])
        msp.add_line((tx, ty), (ax, ay), dxfattribs={"lineweight": LW_THIN})
        msp.add_text(
            label,
            height=float(c.get("height", 3.5)),
            dxfattribs={"style": "OpenSans", "lineweight": LW_INNER},
        ).set_placement((tx, ty))

    out_dir.mkdir(parents=True, exist_ok=True)
    dxf_path = out_dir / f"{stem}.dxf"
    png_path = out_dir / f"{stem}.png"
    doc.saveas(dxf_path)

    # qsave 会把视口与留白算好。手动搭 figure 时 finalize 只做后端收尾、
    # 视口要自己算，漏掉就存出一张纯白图（实测踩过）。
    qsave(
        msp,
        str(png_path),
        bg="#FFFFFF",     # 白底：专利附图不许有底色
        fg="#000000",     # 全黑线条：忽略 DXF 图层颜色，杜绝彩色混入
        dpi=int(spec.get("dpi", 200)),
        size_inches=tuple(spec.get("size_inches", (8, 4.5))),
    )
    return {"dxf": str(dxf_path), "png": str(png_path)}


def main() -> int:
    ap = argparse.ArgumentParser(description="由结构描述生成专利规范的黑白线条图")
    ap.add_argument("--spec", required=True, help="结构描述 JSON 路径")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--stem", default="figure")
    args = ap.parse_args()

    try:
        spec = json.loads(Path(args.spec).read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        print(f"读取 spec 失败: {exc}", file=sys.stderr)
        return 2

    try:
        produced = build(spec, Path(args.out_dir), args.stem)
    except ImportError as exc:
        print(f"缺少依赖（需要 ezdxf 与 matplotlib）: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    # 机读输出，与本项目其它 CLI 工具的约定一致
    print("CAD_LINEART_JSON:" + json.dumps(produced, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
