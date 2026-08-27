"""自检补丁通道测试（prompt-porting-spec §3.3）。

四类场景 + 应用率：
- 精确匹配（anchor_before + original_excerpt 逐字命中）；
- 模糊匹配（模型把换行/空格吐错，空白归一化后仍能定位）；
- 失败（摘录不在正文中 / 摘录为空 / 替换与原文相同）→ 丢弃并进 unresolved；
- 重叠（两条补丁改同一段文字）→ 后者丢弃；
- 应用率 <80% 时 low_rate=True，由调用方决定是否整章重写。
"""

from __future__ import annotations

from itertools import pairwise

from app.models.disclosure import SelfCheckPatch, SelfCheckReport
from app.services import patches as patches_service

CHAPTER = """### 3.4 主流程

S1 采集节点资源画像，得到各节点的 CPU 可用比例与内存可用比例。
S2 按匹配分 b_i^{cpu} 对候选节点排序，取分值最高者派发任务。
S3 若两次重排间隔不足限频阈值，则跳过本轮重排。

### 3.5 关键技术参数

限频阈值 T_r 取 0.5-1.5 秒；权重 α 取 0.6。
"""


def _patch(**kwargs) -> dict:
    base = {
        "checklist_item": "8.2 维度下标",
        "anchor_before": "",
        "original_excerpt": "",
        "replacement": "",
        "reason": "测试补丁",
    }
    base.update(kwargs)
    return base


def test_exact_match_applies():
    """精确匹配：anchor + 逐字摘录命中并替换。"""
    result = patches_service.apply_patches(
        CHAPTER,
        [
            _patch(
                anchor_before="S2 按匹配分 ",
                original_excerpt="b_i^{cpu}",
                replacement="b_{i,\\mathrm{cpu}}",
            )
        ],
    )
    assert result["applied"] == 1
    assert result["rate"] == 1.0
    assert result["low_rate"] is False
    assert result["unresolved"] == []
    assert "b_{i,\\mathrm{cpu}}" in result["text"]
    assert "b_i^{cpu}" not in result["text"]
    # 其余正文原样保留
    assert "S1 采集节点资源画像" in result["text"]


def test_fuzzy_whitespace_match_applies():
    """模糊匹配：摘录里的空白与原文不一致，归一化后仍应命中。"""
    result = patches_service.apply_patches(
        CHAPTER,
        [
            _patch(
                checklist_item="8.1 实施例",
                # 原文是单行、单空格；模型吐成了带换行与多余空格的版本
                original_excerpt="S3 若两次重排间隔不足限频阈值，\n  则跳过本轮重排。",
                replacement="S3 若两次重排间隔不足限频阈值 T_r，则跳过本轮重排。",
            )
        ],
    )
    assert result["applied"] == 1
    assert "不足限频阈值 T_r，则跳过" in result["text"]
    assert result["dropped"] == []


def test_miss_and_empty_are_dropped():
    """未命中 / 空摘录 / 无变化：全部丢弃并写入 unresolved，正文不动。"""
    result = patches_service.apply_patches(
        CHAPTER,
        [
            _patch(original_excerpt="正文里根本没有这段话", replacement="X"),
            _patch(original_excerpt="   ", replacement="Y"),
            _patch(original_excerpt="限频阈值 T_r", replacement="限频阈值 T_r"),
        ],
    )
    assert result["applied"] == 0
    assert result["rate"] == 0.0
    assert result["low_rate"] is True
    assert result["text"] == CHAPTER
    reasons = [d["reason"] for d in result["dropped"]]
    assert reasons == [
        patches_service.REASON_MISS,
        patches_service.REASON_EMPTY,
        patches_service.REASON_NOOP,
    ]
    assert len(result["unresolved"]) == 3


def test_overlapping_patches_second_dropped():
    """重叠补丁：后一条与已接受区间相交 → 丢弃（契约要求摘录互不重叠）。"""
    result = patches_service.apply_patches(
        CHAPTER,
        [
            _patch(original_excerpt="按匹配分 b_i^{cpu} 对候选节点排序", replacement="按匹配分排序"),
            _patch(original_excerpt="b_i^{cpu}", replacement="b_{i,\\mathrm{cpu}}"),
        ],
    )
    assert result["applied"] == 1
    assert len(result["dropped"]) == 1
    assert result["dropped"][0]["reason"] == patches_service.REASON_OVERLAP
    assert result["rate"] == 0.5
    assert result["low_rate"] is True
    assert "按匹配分排序" in result["text"]


def test_multiple_patches_apply_rate_and_offsets():
    """多补丁：全部命中且偏移互不干扰（倒序替换），应用率 1.0。"""
    result = patches_service.apply_patches(
        CHAPTER,
        [
            _patch(original_excerpt="S1 采集节点资源画像", replacement="S1 采集节点资源画像（周期采样）"),
            _patch(original_excerpt="b_i^{cpu}", replacement="b_{i,\\mathrm{cpu}}"),
            _patch(original_excerpt="权重 α 取 0.6", replacement="权重 α 取 0.6（范围 0.5-0.8）"),
        ],
    )
    assert result["applied"] == 3
    assert result["rate"] == 1.0
    text = result["text"]
    assert "（周期采样）" in text
    assert "b_{i,\\mathrm{cpu}}" in text
    assert "（范围 0.5-0.8）" in text
    # spans 是**原文**坐标系，且互不重叠
    spans = sorted((s["start"], s["end"]) for s in result["spans"])
    assert all(a[1] <= b[0] for a, b in pairwise(spans))


def test_apply_rate_threshold():
    """应用率恰好 80% 不算低；低于 80% 才建议整章重写。"""
    ok_patch = _patch(original_excerpt="限频阈值 T_r 取 0.5-1.5 秒", replacement="限频阈值 T_r 取 0.8-1.2 秒")
    miss = _patch(original_excerpt="不存在的段落", replacement="X")
    four_ok = [
        _patch(original_excerpt="S1 采集节点资源画像", replacement="S1 采集资源画像"),
        _patch(original_excerpt="b_i^{cpu}", replacement="b_{i,\\mathrm{cpu}}"),
        _patch(original_excerpt="S3 若两次重排间隔", replacement="S3 若相邻两次重排间隔"),
        ok_patch,
    ]
    result = patches_service.apply_patches(CHAPTER, [*four_ok, miss])
    assert result["applied"] == 4
    assert result["rate"] == 0.8
    assert result["low_rate"] is False

    result2 = patches_service.apply_patches(CHAPTER, [*four_ok[:3], miss, miss])
    assert result2["rate"] == 0.6
    assert result2["low_rate"] is True


def test_pydantic_patches_and_report_entry():
    """契约模型直通：SelfCheckPatch / SelfCheckReport 可直接喂入，unresolved 合并。"""
    report = SelfCheckReport(
        verdict="patched",
        patches=[
            SelfCheckPatch(
                checklist_item="8.2 装饰音",
                anchor_before="S2 按匹配分 ",
                original_excerpt="b_i^{cpu}",
                replacement="b_{i,\\mathrm{cpu}}",
                reason="上标写维度须改下标",
            ),
            SelfCheckPatch(checklist_item="8.3 图示", original_excerpt="不存在", replacement="X"),
        ],
        unresolved=["缺少节点画像采集周期的技术事实"],
    )
    result = patches_service.apply_patch_report(CHAPTER, report)
    assert result["verdict"] == "patched"
    assert result["applied"] == 1
    assert result["rate"] == 0.5
    # 模型自报的 unresolved 在前，服务端丢弃说明在后
    assert result["unresolved"][0].startswith("缺少节点画像采集周期")
    assert "8.3 图示" in result["unresolved"][1]


def test_empty_patch_list_is_noop():
    """无补丁：正文原样返回，应用率按 1.0 计（verdict=pass 的常态）。"""
    result = patches_service.apply_patches(CHAPTER, [])
    assert result == {
        "text": CHAPTER,
        "applied": 0,
        "dropped": [],
        "rate": 1.0,
        "unresolved": [],
        "total": 0,
        "low_rate": False,
        "spans": [],
    }
