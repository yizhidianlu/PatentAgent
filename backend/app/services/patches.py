"""自检补丁通道（prompt-porting-spec §3.3）。

AUDIT 调用产出的补丁清单：

    {verdict, patches:[{checklist_item, anchor_before, original_excerpt, replacement, reason}],
     unresolved[]}

本模块把补丁**确定性地**应用到一段纯文本（交底书全文或单章），返回：

    {text, applied, dropped, rate, unresolved, total, low_rate, spans}

定位顺序（与 `services/faithfulness.locate` 同一实现，避免两套模糊匹配语义漂移）：

1. `anchor_before` 之后的精确匹配；
2. 全文精确匹配；
3. 空白归一化（去掉所有空白字符）后的模糊匹配 —— 覆盖模型把换行/空格吐错的情形。

其余纪律：

- **全部补丁先在原文上定位**，再按起点倒序替换 —— 保证前一处替换不移动后一处的偏移，
  也使重叠检测基于同一坐标系；
- **重叠检测**：某补丁的命中区间与已接受区间相交（含完全相同）时丢弃该补丁，
  记 `overlap` —— 契约要求各 `original_excerpt` 互不重叠，重叠即模型给错了；
- 匹配不到 / 摘录为空 / 替换与原文相同 → 丢弃并写入 `unresolved`（呈现给用户，不入正文）；
- `rate = applied / total`（total=0 时为 1.0）；`rate < 0.80` 置 `low_rate=True`，
  由调用方决定是否整章重写（§3.3 兜底）。

本模块只做文本改写：不碰 DB、不发 SSE、不调 LLM，便于单测。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .faithfulness import locate

logger = logging.getLogger(__name__)

# 补丁应用率下限：低于此值判为「补丁多数落空」，建议整章重写（§3.3）
PATCH_APPLY_FLOOR = 0.80

# 丢弃原因（机读键）
REASON_EMPTY = "empty_excerpt"
REASON_MISS = "not_found"
REASON_OVERLAP = "overlap"
REASON_NOOP = "no_change"

_REASON_ZH = {
    REASON_EMPTY: "original_excerpt 为空",
    REASON_MISS: "锚点/原文片段未在正文中命中（须为逐字子串）",
    REASON_OVERLAP: "与前一条补丁的修改区间重叠",
    REASON_NOOP: "替换文字与原文相同（无实际修改）",
}


@dataclass
class Patch:
    """一条自检补丁（§3.3 契约的服务端内部表示）。"""

    checklist_item: str = ""
    anchor_before: str = ""
    original_excerpt: str = ""
    replacement: str = ""
    reason: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """回吐为契约字段（供 dropped 明细与前端展示）。"""
        return {
            "checklist_item": self.checklist_item,
            "anchor_before": self.anchor_before,
            "original_excerpt": self.original_excerpt,
            "replacement": self.replacement,
            "reason": self.reason,
            **self.extra,
        }


def coerce_patch(item: Any) -> Patch:
    """把 dict / pydantic 模型 / Patch / 任意带属性的对象统一成 Patch。"""
    if isinstance(item, Patch):
        return item
    data: Mapping[str, Any]
    if isinstance(item, Mapping):
        data = item
    elif hasattr(item, "model_dump"):        # pydantic v2（SelfCheckPatch 等）
        data = item.model_dump()
    elif hasattr(item, "__dict__"):
        data = vars(item)
    else:  # pragma: no cover —— 不可解析的补丁项
        raise TypeError(f"无法解析的补丁类型：{type(item).__name__}")

    known = {"checklist_item", "anchor_before", "original_excerpt", "replacement", "reason"}
    return Patch(
        checklist_item=str(data.get("checklist_item") or ""),
        anchor_before=str(data.get("anchor_before") or ""),
        original_excerpt=str(data.get("original_excerpt") or ""),
        replacement=str(data.get("replacement") or ""),
        reason=str(data.get("reason") or ""),
        extra={k: v for k, v in data.items() if k not in known},
    )


def coerce_patches(items: Iterable[Any] | None) -> list[Patch]:
    """批量转换（None 视为空清单）。"""
    return [coerce_patch(x) for x in (items or [])]


def _unresolved_note(patch: Patch, reason_key: str) -> str:
    """丢弃补丁的人类可读说明（并入 unresolved 呈现给用户）。"""
    head = patch.checklist_item.strip() or "自检补丁"
    excerpt = " ".join(patch.original_excerpt.split())[:40]
    tail = f"：「{excerpt}…」" if excerpt else ""
    return f"[{head}] 未应用（{_REASON_ZH.get(reason_key, reason_key)}）{tail}"


def _overlaps(span: tuple[int, int], taken: list[tuple[int, int]]) -> bool:
    """区间是否与任一已接受区间相交（零长区间按点相交处理）。"""
    s, e = span
    for ts, te in taken:
        if s < te and ts < e:
            return True
        if s == e and ts <= s <= te:     # 空替换点落在已改区间内
            return True
    return False


def apply_patches(
    text: str,
    patches: Iterable[Any] | None,
    *,
    floor: float = PATCH_APPLY_FLOOR,
) -> dict[str, Any]:
    """把补丁清单确定性地应用到 `text`。

    参数
    ----
    text     : 被修订的正文（交底书全文或单章）。
    patches  : 补丁清单（dict / pydantic SelfCheckPatch / Patch 混装皆可）。
    floor    : 应用率下限，低于则 `low_rate=True`（默认 0.80）。

    返回
    ----
    ```
    {
      "text":       str,                  # 修订后的正文（无可应用补丁时原样返回）
      "applied":    int,                  # 成功应用条数
      "dropped":    [{"index", "patch", "reason", "reason_zh"}],
      "rate":       float,                # applied / total，total=0 时 1.0
      "unresolved": [str],                # 未应用补丁的说明（呈现给用户，不入正文）
      "total":      int,
      "low_rate":   bool,                 # total>0 且 rate<floor → 建议整章重写
      "spans":      [{"index", "start", "end"}],   # 各补丁在**原文**中的命中区间
    }
    ```
    """
    original = text or ""
    items = coerce_patches(patches)
    total = len(items)

    dropped: list[dict[str, Any]] = []
    unresolved: list[str] = []
    accepted: list[tuple[int, tuple[int, int], Patch]] = []   # (index, span, patch)
    taken: list[tuple[int, int]] = []

    def _drop(index: int, patch: Patch, reason_key: str) -> None:
        dropped.append(
            {
                "index": index,
                "patch": patch.to_dict(),
                "reason": reason_key,
                "reason_zh": _REASON_ZH.get(reason_key, reason_key),
            }
        )
        unresolved.append(_unresolved_note(patch, reason_key))

    for i, patch in enumerate(items):
        if not patch.original_excerpt.strip():
            _drop(i, patch, REASON_EMPTY)
            continue
        span = locate(original, patch.original_excerpt, patch.anchor_before)
        if span is None:
            _drop(i, patch, REASON_MISS)
            continue
        if _overlaps(span, taken):
            _drop(i, patch, REASON_OVERLAP)
            continue
        if original[span[0] : span[1]] == patch.replacement:
            _drop(i, patch, REASON_NOOP)
            continue
        accepted.append((i, span, patch))
        taken.append(span)

    # 倒序替换：前一处改动不移动后一处的偏移
    new_text = original
    for _, (start, end), patch in sorted(accepted, key=lambda x: x[1][0], reverse=True):
        new_text = new_text[:start] + patch.replacement + new_text[end:]

    applied = len(accepted)
    rate = 1.0 if total == 0 else applied / total
    if dropped:
        logger.info("自检补丁：应用 %s/%s，丢弃 %s 条", applied, total, len(dropped))

    return {
        "text": new_text,
        "applied": applied,
        "dropped": dropped,
        "rate": round(rate, 3),
        "unresolved": unresolved,
        "total": total,
        "low_rate": total > 0 and rate < floor,
        "spans": [{"index": i, "start": s, "end": e} for i, (s, e), _ in accepted],
    }


def apply_patch_report(
    text: str,
    report: Any,
    *,
    floor: float = PATCH_APPLY_FLOOR,
) -> dict[str, Any]:
    """`SelfCheckReport`（或同形 dict）直接入口：合并模型自报的 unresolved。

    模型侧 `unresolved[]`（缺技术事实等无法自修的问题）与服务端因未命中而丢弃的补丁
    说明合并为同一份 `unresolved`，由调用方一并呈现给用户 —— 两者都**不入正文**。
    """
    if isinstance(report, Mapping):
        patches = report.get("patches")
        model_unresolved = [str(x) for x in (report.get("unresolved") or [])]
        verdict = str(report.get("verdict") or "")
    else:
        patches = getattr(report, "patches", None)
        model_unresolved = [str(x) for x in (getattr(report, "unresolved", None) or [])]
        verdict = str(getattr(report, "verdict", "") or "")

    result = apply_patches(text, patches, floor=floor)
    result["unresolved"] = model_unresolved + result["unresolved"]
    result["verdict"] = verdict
    return result
