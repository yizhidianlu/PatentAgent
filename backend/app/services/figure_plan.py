"""交底附图选用与排序（模块 A · A3b figure_plan，prompt-porting-spec §2 A3b）。

职责：把案件下已上传的图片逐张打分，产出 `figure_plan`（合同见
`assets/references/schemas/figure_plan.schema.yaml`），并用**服务端确定性规则**
兜住三条硬纪律（prompt 有规定，服务端再兜一次）：

1. **CAD 永不入文** —— `kind: cad` 一律 `use_in_disclosure=False`，`reason` 写明
   「CAD 投影，仅作生图材料」；
2. **合格线 score≥70** —— 低于合格线的图不入文；
3. **入文规则按类型** —— 外观 = 干净实拍（`photo_clean`）+ 合格线稿（`lineart`）都入文；
   实用新型 = 仅合格线稿入文（实拍/CAD 只作生图参考）。

另外：

- **识图能力位缺失时降级**（默认）：模型配置里没有 vision 标记时，本模块仍走一次
  STRUCT 打分调用，但只注入**文件名 / 图注 / 用户补充信息**（不注入图像），并在
  卡片里提示用户可人工调整（`degraded=True`）；
- **线稿本期不做 AI 生成**：缺入文线稿时产出「线稿绘制说明」（模板取自
  `references/schemas/{structure,design}_lineart_brief.schema.yaml` 的 gen_prompt
  约定与 forbid 清单），请用户线下绘制后上传；外观仅有实拍无线稿时明示风险但允许继续。

本模块只做「打分 + 规则 + 文本装配」，不注册步骤、不落交付物。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from ..db import database as db
from ..models.disclosure import FigurePlan
from . import assembler, assets_loader

logger = logging.getLogger(__name__)

# 合格线（figure_plan.schema.yaml：score>=70 才可入文）
PASS_SCORE = 70

# 图片扩展名（与 services/convert.py 的 _IMAGE_EXTS 同口径）
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".tif", ".tiff"}

# 入文白名单（其余 kind 一律不入文）
USABLE_KINDS: dict[str, tuple[str, ...]] = {
    "utility_model": ("lineart",),
    "design": ("lineart", "photo_clean"),
}

PLAN_PARTS = ["common/system_base", "references/schemas/figure_plan.schema.yaml"]

ROLE_LABEL = {
    "assembly": "总装图",
    "detail": "局部图",
    "ortho": "正投影图",
    "perspective": "立体图",
    "reference": "参考图",
    "rejected": "不入文",
}

KIND_LABEL = {
    "lineart": "线稿",
    "cad": "CAD 投影",
    "photo_clean": "干净实拍",
    "photo_scene": "场景实拍",
    "other": "其它",
}


# ---------------------------------------------------------------------------
# 案件图片
# ---------------------------------------------------------------------------


def _image_rows_sync(case_id: str) -> list[dict[str, Any]]:
    """案件下的图片上传件（按上传时间升序）。"""
    rows = db.query_all(
        "SELECT * FROM files WHERE case_id=? AND kind IN ('upload','extracted_figure') "
        "ORDER BY created_at ASC",
        (case_id,),
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        path = Path(str(item.get("stored_path") or ""))
        mime = str(item.get("mime") or "")
        if path.suffix.lower() not in IMAGE_EXTS and not mime.startswith("image/"):
            continue
        try:
            meta = json.loads(item.get("meta_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            meta = {}
        out.append(
            {
                "file_id": str(item.get("id") or ""),
                "orig_name": str(item.get("orig_name") or path.name),
                "path": str(path),
                "exists": path.is_file(),
                "size": int(item.get("size") or 0),
                "caption": str(meta.get("caption") or meta.get("note") or ""),
                "meta": meta,
            }
        )
    return out


async def load_case_images(case_id: str) -> list[dict[str, Any]]:
    """案件下可用于 figure_plan 的图片清单（异步包装）。"""
    return await db.arun(_image_rows_sync, case_id)


def vision_available() -> bool:
    """模型配置是否带多模态识图能力位（缺失即降级为文件名/图注打分）。

    能力位读的是 `settings` 表里 LLM 配置的原始 JSON —— 该键当前不在
    `LlmSettings` 字段表内（`extra="ignore"` 会丢掉），故此处直读原始设置，
    等设置页补上开关后自动生效，无需改本模块。
    """
    try:
        raw = db.get_setting_json("llm") or {}
    except Exception as exc:  # noqa: BLE001 —— 设置读不到即按无识图能力降级
        logger.debug("读取 LLM 设置失败，按无识图能力降级：%s", exc)
        return False
    for key in ("supports_vision", "vision", "supports_image_input", "multimodal"):
        if bool(raw.get(key)):
            return True
    return False


# ---------------------------------------------------------------------------
# 文件名 / 图注启发式（无识图能力时的打分底稿，也作 prompt 提示）
# ---------------------------------------------------------------------------

_KIND_HINTS: list[tuple[str, tuple[str, ...]]] = [
    ("lineart", ("线稿", "线图", "轮廓图", "白描", "专利附图", "附图", "lineart", "line_art",
                 "sketch", "outline", "drawing")),
    ("cad", ("cad", "step", "stp", "iges", "igs", "dwg", "dxf", "三维模型", "建模", "solidworks",
             "proe", "catia")),
    ("photo_scene", ("场景", "实景", "展台", "展会", "广告", "包装", "海报", "banner", "宣传",
                     "使用场景", "scene", "poster")),
    ("photo_clean", ("实拍", "白底", "产品图", "样机", "照片", "photo", "img", "image", "jpg", "jpeg")),
]

_ROLE_HINTS: list[tuple[str, tuple[str, ...]]] = [
    ("assembly", ("总装", "装配", "assembly", "整机")),
    ("detail", ("局部", "细节", "放大", "detail", "卡扣", "钩部", "缺口")),
    ("ortho", ("主视", "俯视", "仰视", "左视", "右视", "后视", "正投影", "正面", "背面",
               "三视", "六视", "ortho", "view")),
    ("perspective", ("立体", "透视", "perspective", "3d")),
    ("reference", ("参考", "reference", "场景", "包装")),
]


def guess_kind(name: str, caption: str = "") -> str:
    """按文件名/图注猜 `kind`（识图能力缺失时的兜底判定）。"""
    blob = f"{name} {caption}".lower()
    for kind, words in _KIND_HINTS:
        if any(w in blob for w in words):
            return kind
    return "other"


def guess_role(name: str, caption: str = "", kind: str = "other") -> str:
    """按文件名/图注猜 `role`。"""
    blob = f"{name} {caption}".lower()
    for role, words in _ROLE_HINTS:
        if any(w in blob for w in words):
            return role
    return "reference" if kind in ("photo_scene", "cad", "other") else "assembly"


def heuristic_scores(name: str, caption: str, kind: str, keywords: Sequence[str] = ()) -> tuple[int, int]:
    """文件名/图注启发式打分 → (relevance, quality)。"""
    blob = f"{name} {caption}"
    relevance = 60
    if any(str(k).strip() and str(k) in blob for k in keywords):
        relevance += 20
    quality = 60
    if kind == "lineart":
        quality += 25
    elif kind == "photo_clean":
        quality += 15
    elif kind == "photo_scene":
        relevance -= 25
        quality -= 20
    elif kind == "cad":
        quality += 5
    else:
        quality -= 10
    if any(w in blob for w in ("模糊", "遮挡", "反光", "blur")):
        quality -= 25
    return max(0, min(100, relevance)), max(0, min(100, quality))


def candidate_cards(images: Sequence[Mapping[str, Any]], keywords: Sequence[str] = ()) -> list[dict[str, Any]]:
    """图片清单 → 注入 STRUCT 调用的候选卡（含启发式提示，供模型参考/覆盖）。"""
    cards: list[dict[str, Any]] = []
    for img in images:
        name = str(img.get("orig_name") or "")
        caption = str(img.get("caption") or "")
        kind = guess_kind(name, caption)
        relevance, quality = heuristic_scores(name, caption, kind, keywords)
        cards.append(
            {
                "path": name,
                "file_id": str(img.get("file_id") or ""),
                "caption": caption,
                "size_kb": round(int(img.get("size") or 0) / 1024, 1),
                "hint_kind": kind,
                "hint_role": guess_role(name, caption, kind),
                "hint_relevance": relevance,
                "hint_quality": quality,
            }
        )
    return cards


def _fallback_figures(cards: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """模型未产出任何条目时的兜底清单（全部按启发式填，交用户在卡片里改）。"""
    out: list[dict[str, Any]] = []
    for card in cards:
        relevance = int(card.get("hint_relevance") or 0)
        quality = int(card.get("hint_quality") or 0)
        out.append(
            {
                "fig": None,
                "role": card.get("hint_role") or "reference",
                "path": card.get("path") or "",
                "covers": [],
                "kind": card.get("hint_kind") or "other",
                "relevance": relevance,
                "quality": quality,
                "score": round((relevance + quality) / 2),
                "use_in_disclosure": False,
                "reason": "平台按文件名与图注启发式打分（未获模型评分），请人工核对",
                "relates_to": [],
            }
        )
    return out


# ---------------------------------------------------------------------------
# STRUCT 打分调用
# ---------------------------------------------------------------------------


def plan_user(
    patent_type: str,
    theme: str,
    cards: Sequence[Mapping[str, Any]],
    schema_digest: Mapping[str, Any] | None,
    *,
    degraded: bool,
    extra_note: str = "",
) -> str:
    """figure_plan 的阶段指令（候选卡 + schema 摘要 + 降级说明）。"""
    lines = [
        "请为本案输出 **figure_plan**（仅一个 JSON 对象，字段与合同同名）。",
        "",
        f"【专利类型】{patent_type}",
        f"【本案主题】{theme or '（暂无）'}",
        "",
        "硬性要求：",
        "1. `figures` 逐条对应下方候选图，`path` **逐字照抄**候选卡的 `path`，不得新增或改写路径；",
        "2. 每条给出 `kind` / `relevance` / `quality` / `score` / `role` / `use_in_disclosure` / `reason`；"
        "`score` 与 `0.5*relevance+0.5*quality` 一致；",
        "3. **合格线 `score>=70`**；`kind: cad` 一律不得入文（`reason` 写「CAD 投影，仅作生图材料」）；",
        "4. 入文规则：**外观**＝干净实拍与合格线稿都入文；**实用新型**＝仅合格线稿入文；"
        "重场景/广告图默认 `role: reference` 且不入文；",
        "5. 有「总装 + 局部」或多视入文时，局部/多视条目必须写 `relates_to`"
        "（`detail_of` / `section_of` / `exploded_of` / `same_state` / `alternate_view` / `sequence`）；",
        "6. `covers` 实用新型填 `parts.id`，外观填视图名或要点短标签。",
    ]
    if degraded:
        lines += [
            "",
            "**注意：本次调用未启用多模态识图**（模型配置无 vision 能力位）。"
            "请**仅依据文件名、图注与材料补充信息**判断，判断不确定时把 `score` 压到合格线以下并在 "
            "`reason` 里写明「未识图，需人工确认」，**不要臆造图面内容**。",
        ]
    if extra_note:
        lines += ["", f"【用户补充信息】{extra_note}"]
    if schema_digest:
        lines += ["", "【事实合同摘要】", json.dumps(schema_digest, ensure_ascii=False, indent=2)]
    lines += ["", "【候选图清单】", json.dumps(list(cards), ensure_ascii=False, indent=2)]
    return "\n".join(lines)


async def plan_figures(
    ctx: Any,
    *,
    patent_type: str,
    images: Sequence[Mapping[str, Any]],
    theme: str = "",
    schema_digest: Mapping[str, Any] | None = None,
    keywords: Sequence[str] = (),
    extra_note: str = "",
    tag: str = "figure_plan",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """逐图打分产 figure_plan（STRUCT）→ 服务端确定性规则 → (plan_dump, report)。"""
    from . import disclosure_build as build_service  # 延迟导入避免循环依赖

    degraded = not vision_available()
    cards = candidate_cards(images, keywords)
    if not cards:
        plan = {
            "version": 1,
            "patent_type": patent_type,
            "theme_summary": theme,
            "figures": [],
        }
        report = {
            "degraded": degraded,
            "vision": not degraded,
            "candidates": 0,
            "used": 0,
            "adjusted": [],
            "notes": ["本案未上传可用图片，figure_plan 为空（须在事实合同 uncertain 中说明无可用图）"],
        }
        return plan, report

    system = assembler.assemble(
        PLAN_PARTS,
        runtime_ctx={
            "case_card": build_service.case_card(ctx),
            "extra": {
                "patent_type": patent_type,
                "vision_enabled": not degraded,
                "candidates": cards,
            },
        },
    )
    user = plan_user(patent_type, theme, cards, schema_digest, degraded=degraded, extra_note=extra_note)
    try:
        plan_obj: FigurePlan = await build_service.call_struct(
            ctx, FigurePlan, tag=tag, system=system, user=user
        )
        raw = plan_obj.model_dump()
    except Exception as exc:  # noqa: BLE001 —— 打分失败不阻断成文，退回启发式清单
        logger.warning("figure_plan 打分调用失败，退回启发式清单：%s", exc)
        raw = {"version": 1, "patent_type": patent_type, "theme_summary": theme, "figures": []}
    raw.setdefault("patent_type", patent_type)
    raw["patent_type"] = patent_type if patent_type in ("utility_model", "design") else "utility_model"
    raw["theme_summary"] = str(raw.get("theme_summary") or theme)
    if not raw.get("figures"):
        raw["figures"] = _fallback_figures(cards)

    plan, report = enforce_rules(raw, patent_type, images)
    report["degraded"] = degraded
    report["vision"] = not degraded
    report["candidates"] = len(cards)
    if degraded:
        report.setdefault("notes", []).append(
            "当前模型未启用多模态识图，附图打分依据文件名与图注推断，请在卡片中人工核对图序与入文选择"
        )
    return plan, report


# ---------------------------------------------------------------------------
# 服务端确定性规则（CAD 永不入文 / 合格线 / 按类型的入文白名单 / 图序与图际关系）
# ---------------------------------------------------------------------------


def _match_image(path: str, images: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """把模型给的 path 映射回真实上传件（按原名 → 文件名 → file_id）。"""
    probe = str(path or "").strip().replace("\\", "/")
    if not probe:
        return None
    base = probe.rsplit("/", 1)[-1].lower()
    for img in images:
        name = str(img.get("orig_name") or "")
        if name and (name == probe or name.lower() == base):
            return img
        stored = str(img.get("path") or "").replace("\\", "/")
        if stored and (stored == probe or stored.rsplit("/", 1)[-1].lower() == base):
            return img
        if str(img.get("file_id") or "") == probe:
            return img
    return None


def _norm_score(item: Mapping[str, Any]) -> tuple[float, float, float]:
    """归一 relevance / quality / score（score 缺失时按 0.5/0.5 合成）。"""

    def _f(key: str) -> float:
        try:
            return max(0.0, min(100.0, float(item.get(key) or 0)))
        except (TypeError, ValueError):
            return 0.0

    relevance, quality = _f("relevance"), _f("quality")
    score = _f("score")
    if not score and (relevance or quality):
        score = round((relevance + quality) / 2, 1)
    return relevance, quality, score


def enforce_rules(
    plan: Mapping[str, Any], patent_type: str, images: Sequence[Mapping[str, Any]] = ()
) -> tuple[dict[str, Any], dict[str, Any]]:
    """对 figure_plan 施加服务端硬规则；返回 (新 plan, 调整报告)。

    规则（figure_plan.schema.yaml「扫描打分」「排序启发式」）：

    - `kind: cad` → 永不入文；
    - `score < 70` → 不入文；
    - 实用新型入文只取 `lineart`；外观入文取 `lineart` 与 `photo_clean`；
    - 入文条目重新连续编号 `fig`（1..n），`relates_to` 的 fig 引用同步改写，
      指向非入文图的关联被丢弃；
    - `path` 必须能映射回真实上传件，否则不入文（禁止凭空引用图片）。
    """
    usable = USABLE_KINDS.get(patent_type, ("lineart",))
    adjusted: list[dict[str, Any]] = []
    notes: list[str] = []
    items: list[dict[str, Any]] = []

    for index, raw in enumerate(plan.get("figures") or []):
        item = dict(raw) if isinstance(raw, Mapping) else {}
        kind = str(item.get("kind") or "other")
        role = str(item.get("role") or "reference")
        relevance, quality, score = _norm_score(item)
        item["relevance"], item["quality"], item["score"] = relevance, quality, score
        want = bool(item.get("use_in_disclosure"))
        reason = str(item.get("reason") or "").strip()
        name = str(item.get("path") or "")

        matched = _match_image(name, images) if images else None
        if matched is not None:
            item["file_id"] = str(matched.get("file_id") or "")
            item["orig_name"] = str(matched.get("orig_name") or name)
            item["abs_path"] = str(matched.get("path") or "")
            item["path"] = str(matched.get("orig_name") or name)
        else:
            item.setdefault("file_id", "")
            item.setdefault("orig_name", name)
            item.setdefault("abs_path", "")

        blocked: str | None = None
        if images and matched is None:
            blocked = "该路径不在本案上传件中，平台不引用无法核验的图片"
        elif matched is not None and not matched.get("exists", True):
            blocked = "对应图片文件已不存在"
        elif kind == "cad":
            blocked = "CAD 投影，仅作生图材料，不入文"
        elif score < PASS_SCORE:
            blocked = f"评分 {score:g} 低于合格线 {PASS_SCORE}"
        elif kind not in usable:
            blocked = (
                "实用新型入文只选合格线稿（实拍/CAD 仅作生图参考）"
                if patent_type == "utility_model"
                else "外观入文只选干净实拍与合格线稿（场景/广告图仅作参考）"
            )

        if blocked:
            if want:
                adjusted.append({"path": item["path"], "kind": kind, "from": True, "to": False, "why": blocked})
            item["use_in_disclosure"] = False
            item["fig"] = None
            if kind == "cad":
                item["role"] = "reference" if role != "rejected" else role
            elif score < PASS_SCORE or kind not in usable:
                item["role"] = role if role in ("reference", "rejected") else "reference"
            item["reason"] = reason if blocked in reason else (f"{reason}；{blocked}".strip("；") if reason else blocked)
        else:
            item["use_in_disclosure"] = True
            item["reason"] = reason or f"{ROLE_LABEL.get(role, role)}（{KIND_LABEL.get(kind, kind)}），入文"
        item["_index"] = index
        items.append(item)

    # ---- 入文条目重新连续编号，并改写图际关系 ----
    used = [i for i in items if i.get("use_in_disclosure")]
    used.sort(key=lambda i: (i.get("fig") if isinstance(i.get("fig"), int) else 10**6, i["_index"]))
    old_to_new: dict[int, int] = {}
    for new_no, item in enumerate(used, 1):
        old = item.get("fig")
        if isinstance(old, int) and old not in old_to_new:
            old_to_new[old] = new_no
        item["fig"] = new_no
    valid_figs = {i["fig"] for i in used}

    for item in items:
        rels: list[dict[str, Any]] = []
        for rel in item.get("relates_to") or []:
            data = dict(rel) if isinstance(rel, Mapping) else {}
            target = data.get("fig")
            if isinstance(target, int):
                data["fig"] = old_to_new.get(target, target)
            if data.get("fig") in valid_figs and data.get("fig") != item.get("fig"):
                rels.append(data)
        item["relates_to"] = rels
        item.pop("_index", None)

    # 「总装 + 局部」入文对必须有图际关联（合同强制项）
    if any(i.get("role") == "assembly" for i in used) and any(i.get("role") == "detail" for i in used):
        for item in used:
            if item.get("role") == "detail" and not item.get("relates_to"):
                notes.append(
                    f"图{item.get('fig')}为局部图但未写 relates_to，成文时无法与总装图对齐，请在卡片中补全"
                )

    out = dict(plan)
    out["patent_type"] = patent_type
    out["figures"] = items
    report = {
        "used": len(used),
        "total": len(items),
        "adjusted": adjusted,
        "notes": notes,
    }
    return out, report


# ---------------------------------------------------------------------------
# 查询与文本装配
# ---------------------------------------------------------------------------


def used_figures(plan: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """入文图（按 fig 升序）。"""
    figures = [
        dict(f)
        for f in ((plan or {}).get("figures") or [])
        if isinstance(f, Mapping) and f.get("use_in_disclosure")
    ]
    figures.sort(key=lambda f: int(f.get("fig") or 0))
    return figures


def figure_caption(item: Mapping[str, Any]) -> str:
    """图题：「图N 角色（形态）」，供正文与 Word 一致引用。"""
    role = ROLE_LABEL.get(str(item.get("role") or ""), "附图")
    kind = KIND_LABEL.get(str(item.get("kind") or ""), "")
    label = f"{role}（{kind}）" if kind else role
    return f"图{item.get('fig')} {label}"


def figure_digest(plan: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """入文图摘要（注入成文 prompt：图序、角色、覆盖对象、图际关系）。"""
    out: list[dict[str, Any]] = []
    for item in used_figures(plan):
        out.append(
            {
                "fig": item.get("fig"),
                "caption": figure_caption(item),
                "role": item.get("role"),
                "kind": item.get("kind"),
                "covers": list(item.get("covers") or []),
                "relates_to": [
                    {"fig": r.get("fig"), "relation": r.get("relation"), "note": r.get("note") or ""}
                    for r in (item.get("relates_to") or [])
                ],
                "reason": item.get("reason") or "",
            }
        )
    return out


def figure_markdown(plan: Mapping[str, Any] | None, *, heading: str = "") -> str:
    """入文图的 markdown 块（图题 + 图片引用；docx 导出按绝对路径嵌图）。"""
    figures = used_figures(plan)
    if not figures:
        return ""
    lines: list[str] = []
    if heading:
        lines += [heading, ""]
    for item in figures:
        caption = figure_caption(item)
        path = str(item.get("abs_path") or "").strip()
        lines.append(f"**{caption}**")
        lines.append("")
        if path:
            lines.append(f"![{caption}]({Path(path).as_posix()})")
        else:
            lines.append(f"> 图待补：{caption}（未取得可引用的图片文件）")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


_FIG_REF_RE = re.compile(r"(?:如|见|参见)?图\s*(\d+)")


def figure_ref_lint(text: str, plan: Mapping[str, Any] | None) -> list[str]:
    """正文「如图 N」必须 ⊆ figure_plan 的入文图，且入文图都要被引用。"""
    figures = used_figures(plan)
    allowed = {int(f.get("fig") or 0) for f in figures}
    problems: list[str] = []
    body = text or ""
    used_refs = {int(m.group(1)) for m in _FIG_REF_RE.finditer(body)}
    extra = sorted(used_refs - allowed)
    if extra:
        problems.append(
            "正文引用了不在 figure_plan 入文清单中的图号 "
            + "、".join(f"图{n}" for n in extra)
            + "：只准引用清单中 use_in_disclosure 为真的条目"
        )
    missing = sorted(allowed - used_refs)
    if missing:
        problems.append(
            "入文图 " + "、".join(f"图{n}" for n in missing) + " 未在正文中说明：一图一句，说明其展示的内容"
        )
    for item in figures:
        for rel in item.get("relates_to") or []:
            target = rel.get("fig")
            if rel.get("relation") in ("detail_of", "section_of", "exploded_of") and target in allowed:
                pattern = re.compile(rf"图\s*{item.get('fig')}[^。；\n]{{0,40}}图\s*{target}")
                if not pattern.search(body):
                    problems.append(
                        f"图{item.get('fig')} 与 图{target} 的关联（{rel.get('relation')}）未在正文写出，"
                        f"应写成「如图{item.get('fig')}为图{target}的局部/剖视…」"
                    )
    return problems


# ---------------------------------------------------------------------------
# 线稿绘制说明（v1 不做 AI 生成：缺线稿时请用户线下绘制后上传）
# ---------------------------------------------------------------------------

_BRIEF_KEY = {
    "utility_model": "references/schemas/structure_lineart_brief.schema.yaml",
    "design": "references/schemas/design_lineart_brief.schema.yaml",
}

_FALLBACK_FORBID = {
    "utility_model": [
        "无依据的内部剖视臆造",
        "品牌 LOGO / 广告文案",
        "彩色渲染 / 阴影棚拍风",
        "与 structure.parts 不一致的件号",
    ],
    "design": [
        "内部结构 / 电路 / 受力",
        "品牌 LOGO / 广告文案",
        "无依据的背面或底面臆造",
        "彩色渲染 / 阴影棚拍风",
        "尺寸线 / 中心线 / 定位线 / 件号引出线 / 工程剖面阴影",
    ],
}

_YAML_FENCE_RE = re.compile(r"```yaml[ \t]*\r?\n(.*?)```", re.DOTALL)


def brief_spec(patent_type: str) -> dict[str, Any]:
    """线稿描述合同骨架（`*_lineart_brief.schema.yaml` 的 yaml 片段）。"""
    key = _BRIEF_KEY.get(patent_type)
    if key:
        try:
            m = _YAML_FENCE_RE.search(assets_loader.get_text(key))
            if m:
                data = yaml.safe_load(m.group(1))
                if isinstance(data, dict):
                    return data
        except (KeyError, yaml.YAMLError) as exc:  # pragma: no cover —— 资产缺失时用内置副本
            logger.warning("线稿描述合同读取失败（%s），改用内置副本：%s", key, exc)
    return {"forbid": _FALLBACK_FORBID.get(patent_type, [])}


def lineart_gaps(plan: Mapping[str, Any] | None, patent_type: str) -> list[str]:
    """哪些入文位缺合格线稿（外观按视图名，实用新型按角色）。"""
    figures = used_figures(plan)
    if patent_type == "utility_model":
        return [] if any(f.get("kind") == "lineart" for f in figures) else ["总装立体图"]
    lineart = [f for f in figures if f.get("kind") == "lineart"]
    photos = [f for f in figures if f.get("kind") == "photo_clean"]
    if lineart:
        return []
    gaps: list[str] = []
    for item in photos:
        gaps.append(figure_caption(item).split(" ", 1)[-1].split("（", 1)[0] or "立体图")
    return gaps or ["立体图"]


def gen_prompt(patent_type: str, view_name: str, *, product_name: str, goal: str) -> str:
    """线稿 gen_prompt 模板（供用户线下用绘图/生图工具产出后上传）。"""
    if patent_type == "design":
        base = (
            f"黑白线稿，白底，纯轮廓线；产品：{product_name or '本案产品'}；视图：{view_name}；"
            f"表现：{goal or '整体造型与可见装饰线条'}；参照附图（如有参考图请一并给绘图者）；"
            "不加阴影、不加渲染、不加色彩、不加背景。"
        )
    else:
        base = (
            f"黑白结构线稿，白底，纯轮廓线；产品：{product_name or '本案装置'}；视图：{view_name}；"
            f"表现：{goal or '部件形状与连接配合关系'}；参照附图（如有参考图请一并给绘图者）；"
            "件号引出线按部件表标注，编号与部件表逐一对应。"
        )
    forbid = brief_spec(patent_type).get("forbid") or _FALLBACK_FORBID.get(patent_type, [])
    return base + "禁止：" + "；".join(str(f) for f in forbid) + "。"


def lineart_brief(
    patent_type: str,
    *,
    plan: Mapping[str, Any] | None,
    product_name: str = "",
    goals: Mapping[str, str] | None = None,
    parts_legend: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """「线稿绘制说明」：缺线稿时随 schema_fill 卡片下发，请用户线下绘制后上传。"""
    gaps = lineart_gaps(plan, patent_type)
    spec = brief_spec(patent_type)
    goal_map = dict(goals or {})
    views = [
        {
            "view_name": name,
            "lineart_goal": goal_map.get(name, ""),
            "gen_prompt": gen_prompt(patent_type, name, product_name=product_name, goal=goal_map.get(name, "")),
            "output_hint": "绘制/生成后上传为 PNG（白底黑线），平台会重跑附图打分并入文",
        }
        for name in gaps
    ]
    photos = [f for f in used_figures(plan) if f.get("kind") == "photo_clean"]
    risk = ""
    if patent_type == "design" and gaps and photos:
        risk = (
            "本案当前只有干净实拍、没有合格线稿：外观交底可先按实拍推进，"
            "但代理人通常仍需线稿或正式视图，**存在补图风险**；可继续成文，也可上传线稿后重跑本步。"
        )
    elif patent_type == "utility_model" and gaps:
        risk = (
            "本案当前没有合格线稿：实用新型入文只收线稿（CAD/实拍不入文），"
            "第三章将以文字与表格描述结构，附图待补。"
        )
    return {
        "needed": bool(gaps),
        "patent_type": patent_type,
        "product_name": product_name,
        "views": views,
        "forbid": [str(f) for f in (spec.get("forbid") or [])],
        "parts_legend": [dict(p) for p in parts_legend],
        "risk_note": risk,
        "note": "本期不做线稿 AI 生成：请按上述说明线下绘制或用图像工具生成后上传，再重跑本步骤。",
    }


def plan_warnings(plan: Mapping[str, Any] | None, report: Mapping[str, Any] | None, patent_type: str) -> list[str]:
    """卡片提示语（入文数量、被规则拦下的图、降级说明）。"""
    figures = used_figures(plan)
    out: list[str] = []
    if not figures:
        out.append("当前没有任何图片满足入文条件（合格线 70 分 / CAD 不入文 / 按类型的入文白名单）")
    for item in (report or {}).get("adjusted") or []:
        out.append(f"「{item.get('path')}」原标记入文，已按规则改为不入文：{item.get('why')}")
    out.extend(str(n) for n in ((report or {}).get("notes") or []))
    if (report or {}).get("degraded"):
        out.append("识图能力位缺失：以上打分为文件名/图注推断结果，可在本卡片中人工调整")
    return out


def apply_user_plan(
    plan: Mapping[str, Any], payload: Any, patent_type: str, images: Sequence[Mapping[str, Any]] = ()
) -> tuple[dict[str, Any], dict[str, Any]]:
    """把用户在卡片里改过的 figure_plan 合并回来（仍过一遍服务端硬规则）。"""
    if not isinstance(payload, Mapping):
        return dict(plan), {"used": len(used_figures(plan)), "adjusted": [], "notes": []}
    figures = payload.get("figures")
    if not isinstance(figures, list) or not figures:
        return dict(plan), {"used": len(used_figures(plan)), "adjusted": [], "notes": []}
    merged = dict(plan)
    by_path = {str(f.get("path") or ""): dict(f) for f in (plan.get("figures") or []) if isinstance(f, Mapping)}
    out: list[dict[str, Any]] = []
    for raw in figures:
        if not isinstance(raw, Mapping):
            continue
        base = dict(by_path.get(str(raw.get("path") or ""), {}))
        base.update({k: v for k, v in raw.items() if v is not None})
        out.append(base)
    merged["figures"] = out or list(plan.get("figures") or [])
    return enforce_rules(merged, patent_type, images)


def as_plan_model(plan: Mapping[str, Any]) -> FigurePlan:
    """dict → FigurePlan（extra='allow'，保留 file_id / abs_path 等平台扩展键）。"""
    return FigurePlan.model_validate(dict(plan))


def json_text(value: Any) -> str:
    """注入 prompt 的 JSON 文本。"""
    return json.dumps(value, ensure_ascii=False, indent=2)


def keywords_from(*sources: Iterable[Any]) -> list[str]:
    """从标题实词/部件名/设计要点里收集打分关键词。"""
    out: list[str] = []
    for source in sources:
        for item in source or []:
            value = str(item).strip()
            if value and value not in out:
                out.append(value)
    return out
