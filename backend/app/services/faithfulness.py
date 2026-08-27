"""忠实性审计与补丁应用服务（模块 B · B4b / B7）。

三件事：
1. **忠实性审计（AUDIT）**：把草稿的权利要求与具体实施方式逐条断言拿去和论文原文
   （复用 B2 的 source_map）核对，落不了源的进 `unsourced`
   （prompt: `paper2patent/faithfulness_audit.md`）；
2. **质检（AUDIT）**：按 Quality Checklist 五类清单出补丁清单
   （prompt: `paper2patent/quality_check.md`），服务端**确定性应用**补丁——
   锚点+原文片段先精确匹配，失败再做空白归一化的模糊匹配；应用率 <80% 记 unresolved
   （prompt-porting-spec §3.3）；
3. **unsourced 的落地处置**：direct 模式一律改写为 `【待补充：…】` 并入 gaps；
   human-in-loop 模式由用户逐条裁决（delete / placeholder / keep）。

本模块只做「审计 + 文本改写」，不碰 DB、不发 SSE，便于单测。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable, Mapping

from ..models.paper2patent import (
    FaithfulnessAudit,
    QualityAudit,
    QualityPatch,
    UnsourcedAssertion,
)
from . import assembler, llm

logger = logging.getLogger(__name__)

# 补丁应用率下限（低于此值判为「补丁多数落空」，记 unresolved 交人工，§3.3）
PATCH_APPLY_FLOOR = 0.80

# 装配顺序遵循 §0.2：平台角色头 → 模块 Role → 脱敏 → 术语 → 阶段指令
FAITHFULNESS_PARTS = [
    "common/system_base",
    "paper2patent/pro/role_task",
    "common/desensitization",
    "common/terminology_rules",
    "paper2patent/faithfulness_audit",
]

QUALITY_PARTS = [
    "common/system_base",
    "paper2patent/pro/role_task",
    "common/desensitization",
    "common/terminology_rules",
    "paper2patent/quality_check",
]


# ---------------------------------------------------------------------------
# 草稿视图工具
# ---------------------------------------------------------------------------


def claims_text(claims: Iterable[str]) -> str:
    """权利要求列表 → 逐项换行的纯文本（喂给审计调用）。"""
    return "\n".join(str(c).strip() for c in claims if str(c).strip())


def draft_digest(draft: Mapping[str, Any]) -> dict[str, Any]:
    """草稿的审计视图（五部分正文，剔除内部字段）。"""
    desc = draft.get("description") or {}
    return {
        "invention_name": draft.get("invention_name", ""),
        "abstract": draft.get("abstract", ""),
        "abstract_drawing": draft.get("abstract_drawing", ""),
        "claims": list(draft.get("claims") or []),
        "description": {
            "technical_field": desc.get("technical_field", ""),
            "background": desc.get("background", ""),
            "invention_content": desc.get("invention_content", ""),
            "drawing_description": list(desc.get("drawing_description") or []),
            "embodiments": desc.get("embodiments", ""),
        },
        "drawings": list(draft.get("drawings") or []),
    }


# ---------------------------------------------------------------------------
# 审计调用
# ---------------------------------------------------------------------------


async def audit_faithfulness(
    *,
    case_id: str,
    step_key: str,
    paper_text: str,
    source_map: list[dict[str, Any]],
    claims: list[str],
    embodiments: str,
    case_card: Mapping[str, Any] | None = None,
) -> FaithfulnessAudit:
    """B4b 忠实性审计：断言逐条落源，返回 {verdict, mappings, unsourced}。"""
    system = assembler.assemble(
        FAITHFULNESS_PARTS,
        runtime_ctx={
            "case_card": dict(case_card or {}),
            "extra": {"source_map": source_map},
        },
    )
    user = (
        "以下是论文原文（或按预算裁剪后的选段）与当前草稿，请按审计方法逐条核查并只输出审计 JSON。\n\n"
        "===== 论文原文 =====\n"
        f"{paper_text}\n\n"
        "===== 草稿·权利要求书 =====\n"
        f"{claims_text(claims)}\n\n"
        "===== 草稿·具体实施方式 =====\n"
        f"{embodiments}\n"
    )
    return await llm.structured(
        [{"role": "system", "content": str(system)}, {"role": "user", "content": user}],
        FaithfulnessAudit,
        case_id=case_id,
        step_key=step_key,
        **assembler.call_params("AUDIT"),
    )


async def audit_quality(
    *,
    case_id: str,
    step_key: str,
    draft: Mapping[str, Any],
    source_map: list[dict[str, Any]],
    case_card: Mapping[str, Any] | None = None,
) -> QualityAudit:
    """B7 质检：按五类清单核对草稿全文，返回补丁契约 {verdict, patches, unresolved}。"""
    system = assembler.assemble(
        QUALITY_PARTS,
        runtime_ctx={
            "case_card": dict(case_card or {}),
            "extra": {"source_map_digest": source_map[:40]},
        },
    )
    user = (
        "以下是当前草稿全文（五部分）。请逐项核对清单并只输出补丁 JSON；"
        "original_excerpt 必须是草稿中逐字存在的连续片段。\n\n"
        f"{json.dumps(draft_digest(draft), ensure_ascii=False, indent=2)}"
    )
    return await llm.structured(
        [{"role": "system", "content": str(system)}, {"role": "user", "content": user}],
        QualityAudit,
        case_id=case_id,
        step_key=step_key,
        **assembler.call_params("AUDIT"),
    )


# ---------------------------------------------------------------------------
# 定位：精确匹配 → 空白归一化模糊匹配
# ---------------------------------------------------------------------------


def _normalize_with_map(text: str) -> tuple[str, list[int]]:
    """去掉全部空白字符，同时保留每个保留字符在原文中的下标。"""
    chars: list[str] = []
    index: list[int] = []
    for i, ch in enumerate(text):
        if ch.isspace():
            continue
        chars.append(ch)
        index.append(i)
    return "".join(chars), index


def locate(text: str, excerpt: str, anchor: str = "") -> tuple[int, int] | None:
    """在 text 中定位 excerpt，返回 [start, end)；找不到返回 None。

    顺序：锚点后的精确匹配 → 全文精确匹配 → 空白归一化后的模糊匹配（锚点同样归一化）。
    """
    if not text or not excerpt:
        return None

    if anchor:
        a = text.find(anchor)
        if a >= 0:
            p = text.find(excerpt, a + len(anchor))
            if p >= 0:
                return (p, p + len(excerpt))
    p = text.find(excerpt)
    if p >= 0:
        return (p, p + len(excerpt))

    norm, index = _normalize_with_map(text)
    needle = "".join(excerpt.split())
    if not needle:
        return None
    start = -1
    anchor_norm = "".join(anchor.split())
    if anchor_norm:
        a = norm.find(anchor_norm)
        if a >= 0:
            start = norm.find(needle, a + len(anchor_norm))
    if start < 0:
        start = norm.find(needle)
    if start < 0:
        return None
    return (index[start], index[start + len(needle) - 1] + 1)


def _replace_span(text: str, span: tuple[int, int], replacement: str) -> str:
    return text[: span[0]] + replacement + text[span[1] :]


# ---------------------------------------------------------------------------
# unsourced 处置（direct 转占位 / hil 用户裁决）
# ---------------------------------------------------------------------------


def placeholder_text(item: UnsourcedAssertion, *, with_period: bool = True) -> str:
    """把落不了源的断言转成【待补充：…】占位文案。"""
    text = (item.suggested_text or item.assertion or item.location or "").strip()
    if text.startswith("【待补充"):
        return text
    body = text.rstrip("。;；,，").strip()
    tail = "。】" if with_period else "】"
    return f"【待补充：{body}{tail}"


def _with_location(text: str, location: str) -> str:
    """改不动正文时，在 gaps 文案里补上草稿内定位，便于人工回查。"""
    if not location:
        return text
    if text.endswith("】"):
        return f"{text[:-1]}（位于{location}）】"
    return f"{text}（位于{location}）"


def _resolve_target(draft: dict[str, Any], location: str) -> tuple[str, Any] | None:
    """把审计给的草稿内定位映射到具体字段：('claims', idx) / ('embodiments', None)。"""
    loc = location or ""
    m = re.search(r"权利要求\s*(\d+)", loc)
    if m:
        idx = int(m.group(1)) - 1
        claims = draft.get("claims") or []
        if 0 <= idx < len(claims):
            return ("claims", idx)
        return None
    if "实施方式" in loc or "实施例" in loc or "embodiment" in loc.lower():
        return ("embodiments", None)
    if "摘要" in loc:
        return ("abstract", None)
    if "背景" in loc:
        return ("background", None)
    if "发明内容" in loc:
        return ("invention_content", None)
    return None


def _get_field(draft: dict[str, Any], target: tuple[str, Any]) -> str:
    kind, idx = target
    if kind == "claims":
        return str((draft.get("claims") or [])[idx])
    if kind in ("embodiments", "background", "invention_content"):
        return str((draft.get("description") or {}).get(kind, ""))
    return str(draft.get(kind, ""))


def _set_field(draft: dict[str, Any], target: tuple[str, Any], value: str) -> None:
    kind, idx = target
    if kind == "claims":
        claims = list(draft.get("claims") or [])
        claims[idx] = value
        draft["claims"] = claims
        return
    if kind in ("embodiments", "background", "invention_content"):
        desc = dict(draft.get("description") or {})
        desc[kind] = value
        draft["description"] = desc
        return
    draft[kind] = value


def apply_unsourced(
    draft: Mapping[str, Any],
    audit: FaithfulnessAudit,
    *,
    mode: str = "direct",
    decisions: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """按模式处置 unsourced 断言，返回 (新草稿, gaps 文案列表)。

    - direct：一律把断言原文改写为【待补充：…】（改不动就只记 gaps，绝不编造补齐）；
    - human_in_loop：按用户逐条裁决 decisions[i] = {"action": "delete|placeholder|keep",
      "text": 可选的替换文字}；keep 表示用户确认该断言有据，不动正文也不记 gaps。

    权利要求内的替换文案不带句号，避免破坏「每项仅结尾一个句号」的硬规则。
    """
    new_draft: dict[str, Any] = json.loads(json.dumps(dict(draft), ensure_ascii=False))
    gaps: list[str] = []
    decision_list = decisions or []

    for i, item in enumerate(audit.unsourced):
        action = "placeholder"
        custom_text = ""
        if mode == "human_in_loop":
            decision = decision_list[i] if i < len(decision_list) else {}
            action = str(decision.get("action") or "placeholder").lower()
            custom_text = str(decision.get("text") or "").strip()
            if action == "keep":
                continue

        target = _resolve_target(new_draft, item.location)
        in_claim = bool(target and target[0] == "claims")
        # delete=直接删除（用户可给替换文字）；其余情况优先用用户文字，缺省转占位符
        if action == "delete" or custom_text:
            replacement = custom_text
        else:
            replacement = placeholder_text(item, with_period=not in_claim)

        applied = False
        if target is not None and item.assertion:
            text = _get_field(new_draft, target)
            span = locate(text, item.assertion)
            if span is not None:
                _set_field(new_draft, target, _replace_span(text, span, replacement))
                applied = True

        note = placeholder_text(item)
        gaps.append(note if applied else _with_location(note, item.location))

    return new_draft, gaps


# ---------------------------------------------------------------------------
# 质检补丁应用
# ---------------------------------------------------------------------------


def _patch_targets(draft: dict[str, Any], section: str) -> list[tuple[str, Any]]:
    """某 section 下可供匹配的字段目标（按优先顺序）。"""
    if section == "claims":
        return [("claims", i) for i in range(len(draft.get("claims") or []))]
    if section == "drawings":
        return [("drawings", i) for i in range(len(draft.get("drawings") or []))]
    if section == "description":
        desc = draft.get("description") or {}
        targets: list[tuple[str, Any]] = [
            ("description", key)
            for key in ("technical_field", "background", "invention_content", "embodiments")
        ]
        targets += [
            ("drawing_description", i) for i in range(len(desc.get("drawing_description") or []))
        ]
        return targets
    if section in ("abstract", "abstract_drawing"):
        return [(section, None)]
    return []


def _patch_get(draft: dict[str, Any], target: tuple[str, Any]) -> str:
    kind, key = target
    if kind in ("claims", "drawings"):
        return str((draft.get(kind) or [])[key])
    if kind == "description":
        return str((draft.get("description") or {}).get(key, ""))
    if kind == "drawing_description":
        return str(((draft.get("description") or {}).get("drawing_description") or [])[key])
    return str(draft.get(kind, ""))


def _patch_set(draft: dict[str, Any], target: tuple[str, Any], value: str) -> None:
    kind, key = target
    if kind in ("claims", "drawings"):
        items = list(draft.get(kind) or [])
        items[key] = value
        draft[kind] = items
        return
    if kind == "description":
        desc = dict(draft.get("description") or {})
        desc[key] = value
        draft["description"] = desc
        return
    if kind == "drawing_description":
        desc = dict(draft.get("description") or {})
        lines = list(desc.get("drawing_description") or [])
        lines[key] = value
        desc["drawing_description"] = lines
        draft["description"] = desc
        return
    draft[kind] = value


def apply_patches(
    draft: Mapping[str, Any], patches: list[QualityPatch]
) -> dict[str, Any]:
    """把质检补丁确定性地应用到草稿。

    返回 {"draft", "applied", "dropped": [{patch, reason}], "rate", "low_rate"}；
    匹配不到的补丁一律丢弃（§3.3：摘录务必逐字），应用率 <80% 时 low_rate=True，
    由调用方并入 unresolved 呈现给用户。
    """
    new_draft: dict[str, Any] = json.loads(json.dumps(dict(draft), ensure_ascii=False))
    applied = 0
    dropped: list[dict[str, Any]] = []

    for patch in patches:
        if not patch.original_excerpt:
            dropped.append({"patch": patch.model_dump(), "reason": "original_excerpt 为空"})
            continue
        hit = False
        for target in _patch_targets(new_draft, patch.section):
            text = _patch_get(new_draft, target)
            span = locate(text, patch.original_excerpt, patch.anchor_before)
            if span is None:
                continue
            _patch_set(new_draft, target, _replace_span(text, span, patch.replacement))
            applied += 1
            hit = True
            break
        if not hit:
            dropped.append({"patch": patch.model_dump(), "reason": "锚点/原文片段未命中"})

    total = len(patches)
    rate = 1.0 if total == 0 else applied / total
    return {
        "draft": new_draft,
        "applied": applied,
        "dropped": dropped,
        "rate": round(rate, 3),
        "low_rate": total > 0 and rate < PATCH_APPLY_FLOOR,
    }
