"""专利交底书流水线（模块 A，prompt-porting-spec.md §2 模块A / A1–A8）。

发明八步：

    intake(门控) → material_scan → points_mining(门控) → prior_art_search(门控)
        → preview(门控) → build → self_check → deliver(门控)

实用新型 / 外观九步（A3b 填表与线稿插在挖点之后、查新之前）：

    intake(门控) → material_scan → points_mining(门控) → **schema_fill(门控)**
        → prior_art_search(门控) → preview(门控) → build → self_check → deliver(门控)

- **启动载荷**：`{file_ids:[], instruction?, patent_type?}`（file_ids 仅作提示，
  实际以案件下已转换出 markdown 的上传件为准）；
- **类型分派**：`build_steps()` 按 `cases.patent_type` 选步骤工厂；三类型共用同一批
  step_key（发明少一个 `schema_fill`），各步骤内部按类型选 prompt 资产与 lint 束；
- **A3b 填表**：STRUCT 产 StructureSchema / AppearanceSchema + figure_plan 逐图打分
  （合格线 70、CAD 永不入文、实用只收线稿、外观实拍+线稿都入文），
  `not_*_signals` 非空先走类型改判反问；缺线稿时下发「线稿绘制说明」请用户线下绘制上传
  （本期不做线稿 AI 生成）；
- **忠实红线**：查新失败/跳过时如实写明未检索，**禁止**编造检索结果与 URL；
  正文出现的 URL 必须 ∈ 命中链接集合（服务端白名单 lint）；
- **分章生成**：三条分支的生成、lint 与 REPAIR 全部落在 `services/disclosure_build.py`，
  本文件只负责步骤编排、人机门控与交付物落盘。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import anyio.to_thread
from pydantic import ValidationError
from ulid import ULID

from ..config import get_config
from ..db import database as db
from ..models.disclosure import (
    AppearanceSchema,
    ClaimBias,
    Contact,
    IntakeAnswer,
    MaterialDigest,
    PatentPoints,
    SelfCheckReport,
    StructureSchema,
)
from ..models.search import AbstractDigests, SearchTermsPlan
from ..services import artifacts as artifacts_service
from ..services import skills as skills_service
from ..services import assembler, assets_loader, cnipa, faithfulness, terminology
from ..services import disclosure_build as build_service
from ..services import figure_plan as figure_plan_service
from ..services import export_docx as export_docx_service
from ..services import export_pdf as export_pdf_service
from ..services import formula as formula_service
from ..services import patches as patches_service
from ..services.sse import hub
from .engine import Ctx, InteractionRequest, StepDef

logger = logging.getLogger(__name__)

DOC_ID = build_service.DOC_ID

# 材料分片阈值（§2 A2：>3k tokens 分片 map-reduce 归并）
MATERIAL_CHUNK_TOKENS = 3000
# 查新失败后的门控轮数上限（防止 retry 死循环）
MAX_SEARCH_ROUNDS = 3
# 每批消化的命中条目数（abstract_digest.md：每批 ≤8 条）
DIGEST_BATCH = 8
# 查新笔记 / 偏向点 / 自检的 REPAIR 上限
MAX_DIGEST_REPAIR = 2
MAX_TERMS_REPAIR = 2
MAX_BIAS_REPAIR = 1
MAX_SELF_CHECK_ROUNDS = 2
# 摘要预览「调整方向」重跑上限
MAX_PREVIEW_ROUNDS = 3

PATENT_TYPES = ("invention", "utility_model", "design")


class PatentTypeNotSupportedError(RuntimeError):
    """该专利类型的分支尚未实现（当前仅用于迭代流水线：迭代只支持发明）。"""


def _unsupported_message(patent_type: str) -> str:
    """迭代（合并/纠正）暂不支持的类型提示。

    交底书**首轮成文**三类型均已支持（M5）；迭代分支仍只覆盖发明，
    `pipelines/disclosure_iterate.py` 复用本函数产出友好错误。
    """
    label = build_service.PATENT_TYPE_LABEL.get(patent_type, patent_type)
    return (
        f"「{label}」交底书的**迭代**（合并新材料 / 纠正）尚未开放，当前仅支持发明专利交底书的迭代；"
        f"「{label}」的首轮成文可正常使用。如需修改本稿，请重新发起一次成文。"
    )


def _require_invention(ctx: Any) -> None:
    """迭代（合并/纠正）分支的类型守卫：非发明类型抛出友好错误。

    首轮成文三类型均已支持；仅 `disclosure_iterate.py` 复用本守卫。
    """
    ptype = str((getattr(ctx, "case", None) or {}).get("patent_type") or "invention")
    if ptype != "invention":
        raise PatentTypeNotSupportedError(_unsupported_message(ptype))


# ---------------------------------------------------------------------------
# prompt 装配清单（§0.2）
# ---------------------------------------------------------------------------

BASE = ["common/system_base"]
INTAKE_PARTS = [*BASE, "disclosure/intake"]
DIGEST_PARTS = [*BASE, "disclosure/material_digest", "common/desensitization"]
POINTS_PARTS = [*BASE, "disclosure/invention/points", "common/desensitization", "common/terminology_rules"]
SEARCH_TERMS_PARTS = [*BASE, "disclosure/prior_art/search_terms"]
ABSTRACT_DIGEST_PARTS = [*BASE, "disclosure/prior_art/abstract_digest"]
PREVIEW_PARTS = [*BASE, "disclosure/preview", "common/desensitization", "common/terminology_rules"]
SELF_CHECK_PARTS = [
    *BASE,
    "disclosure/self_check_invention",
    "disclosure/invention/formula_style",
    "common/terminology_rules",
]
CLAIM_BIAS_PARTS = [*BASE, "disclosure/invention/claim_bias"]

# 按类型分派的 prompt 装配清单（A3 挖点 / A3b 填表 / A7 自检）
POINTS_PARTS_BY_TYPE: dict[str, list[str]] = {
    "invention": POINTS_PARTS,
    "utility_model": [
        *BASE, "disclosure/utility_model/points", "common/desensitization", "common/terminology_rules",
    ],
    "design": [
        *BASE, "disclosure/design/points", "common/desensitization", "common/terminology_rules",
    ],
}

SCHEMA_FILL_PARTS: dict[str, list[str]] = {
    "utility_model": [*BASE, "disclosure/shared/fill_structure_schema"],
    "design": [*BASE, "disclosure/shared/fill_appearance_schema"],
}

SELF_CHECK_PARTS_BY_TYPE: dict[str, list[str]] = {
    "invention": SELF_CHECK_PARTS,
    "utility_model": [*BASE, "disclosure/self_check_utility", "common/terminology_rules"],
    "design": [*BASE, "disclosure/self_check_design", "common/terminology_rules"],
}

# 成文人格资产（元信息局部修复等零散改写调用按类型取）
BUILDER_PART_BY_TYPE: dict[str, str] = {
    "invention": "disclosure/invention/builder_core",
    "utility_model": "disclosure/utility_model/builder",
    "design": "disclosure/design/builder",
}

# A3 候选点数量（§2 A3：发明 3-5 / 实用 2-4 / 外观 1-3）
POINTS_RANGE = {"invention": "3–5", "utility_model": "2–4", "design": "1–3"}

# A3b 事实合同：类型 → (state 键, 模型, 信号字段)
SCHEMA_SPEC: dict[str, tuple[str, Any, str]] = {
    "utility_model": ("structure_schema", StructureSchema, "not_utility_model_signals"),
    "design": ("appearance_schema", AppearanceSchema, "not_design_signals"),
}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n\s*```", re.DOTALL)


def _tail_json(text: str) -> dict[str, Any] | None:
    """取生成结果末尾的 ```json 围栏块（从后往前找第一个能解析的对象）。"""
    for m in reversed(list(_JSON_FENCE_RE.finditer(text or ""))):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _payload(ctx: Ctx) -> dict[str, Any]:
    return ctx.start_payload if isinstance(ctx.start_payload, dict) else {}


def _answer(value: Any) -> dict[str, Any]:
    """门控回填：非 dict（含 None）一律视为空表单，走缺省分支。"""
    return dict(value) if isinstance(value, Mapping) else {}


def _card(ctx: Ctx) -> dict[str, Any]:
    return build_service.case_card(ctx)


def _patent_type(ctx: Ctx) -> str:
    state = ctx.state or {}
    value = str(state.get("patent_type") or (ctx.case or {}).get("patent_type") or "invention")
    return value if value in PATENT_TYPES else "invention"


def _type_label(patent_type: str) -> str:
    return build_service.PATENT_TYPE_LABEL.get(patent_type, "发明")


async def _emit_artifact(case_id: str, artifact) -> None:
    await hub.emit(case_id, "artifact_created", json.loads(artifact.model_dump_json()))


async def _emit_doc_version(case_id: str, artifact, downloads: list[dict[str, Any]] | None = None) -> None:
    await hub.emit(
        case_id,
        "doc_version",
        {
            "doc_id": DOC_ID,
            "version_id": artifact.id,
            "version": artifact.version,
            "kind": artifact.kind,
            "filename": artifact.filename,
            "artifact_id": artifact.id,
            "downloads": downloads
            or [{"kind": artifact.kind, "artifact_id": artifact.id, "filename": artifact.filename}],
            "iteration_type": artifact.iteration_type,
            "summary": artifact.summary,
            "created_at": artifact.created_at,
        },
    )


# ---------------------------------------------------------------------------
# 步骤 1：intake —— 边界录入 [PAUSE] + CHAT 复述
# ---------------------------------------------------------------------------

INTAKE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "topic": {
            "type": "string",
            "title": "技术主题或产品模块（一句话）",
            "description": "例如「多模态检索排序」「工业质检缺陷分级」「集成式电驱桥壳体」。",
        },
        "patent_type": {
            "type": "string",
            "title": "专利类型",
            "enum": ["invention", "utility_model", "design", "unknown"],
            "enumNames": ["发明", "实用新型", "外观设计", "暂不确定"],
            "default": "invention",
        },
        "contact": {
            "type": "object",
            "title": "文头技术联系人（可留空写「待填写」）",
            "properties": {
                "name": {"type": "string", "title": "姓名"},
                "phone": {"type": "string", "title": "电话"},
                "email": {"type": "string", "title": "邮箱"},
            },
        },
        "notes": {"type": "string", "title": "其他边界说明（可选）"},
    },
}

INTAKE_PROMPT = (
    "为便于挖掘专利点与组织交底书，先确认几项边界；可跳过，将按已有材料推断。\n"
    "① 请用一句话描述本方案所属技术主题或产品模块；\n"
    "② 本案倾向哪一类专利？发明 / 实用新型 / 外观设计 / 暂不确定"
    "（未显式指定时统一默认「发明」；方法 / 系统 / 装置属于权利要求书式倾向，不是专利类型）；\n"
    "③ 交底书文头的技术联系人是否需要占位？需要则填写姓名/电话/邮箱，不需要则留空写「待填写」。"
)


def _default_intake(ctx: Ctx) -> dict[str, Any]:
    """门控默认值：从启动载荷与案件行推断。"""
    payload = _payload(ctx)
    case = ctx.case or {}
    ptype = str(payload.get("patent_type") or case.get("patent_type") or "invention")
    try:
        contact = json.loads(case.get("contact_json") or "null") or {}
    except (TypeError, json.JSONDecodeError):
        contact = {}
    return {
        "topic": str(payload.get("instruction") or "").strip() or str(case.get("title") or "").strip(),
        "patent_type": ptype if ptype in PATENT_TYPES else "invention",
        "contact": {
            "name": str(contact.get("name") or "待填写"),
            "phone": str(contact.get("phone") or "待填写"),
            "email": str(contact.get("email") or "待填写"),
        },
        "notes": "",
    }


def _persist_intake_sync(case_id: str, patent_type: str, contact: dict[str, Any]) -> None:
    db.execute(
        "UPDATE cases SET patent_type=?, contact_json=?, updated_at=? WHERE id=?",
        (patent_type, json.dumps(contact, ensure_ascii=False), db.now_str(), case_id),
    )


async def intake(ctx: Ctx) -> dict[str, Any]:
    """A1 边界录入：表单门控 → 写 patent_type/联系人 → CHAT 复述 3-6 行 bullet。"""
    default = _default_intake(ctx)
    payload = _payload(ctx)
    answer = _answer(
        await ctx.await_user(
            InteractionRequest(
                kind="intake", schema=INTAKE_SCHEMA, prompt=INTAKE_PROMPT, default=default
            )
        )
    )

    raw_type = str(answer.get("patent_type") or "").strip()
    explicit = raw_type in PATENT_TYPES
    if not explicit and str(payload.get("patent_type") or "") in PATENT_TYPES:
        # 用户在表单里没改，但建案/启动载荷里显式给过类型
        raw_type, explicit = str(payload["patent_type"]), True
    patent_type = raw_type if explicit else "invention"

    contact_data = _answer(answer.get("contact")) or default["contact"]
    contact = Contact.model_validate(
        {
            "name": str(contact_data.get("name") or "待填写").strip() or "待填写",
            "phone": str(contact_data.get("phone") or "待填写").strip() or "待填写",
            "email": str(contact_data.get("email") or "待填写").strip() or "待填写",
        }
    )
    intake_answer = IntakeAnswer.model_validate(
        {
            "topic": str(answer.get("topic") or default["topic"] or "").strip(),
            "patent_type": patent_type,
            "patent_type_explicit": explicit,
            "contact": contact.model_dump(),
            "notes": str(answer.get("notes") or "").strip(),
        }
    )

    await db.arun(_persist_intake_sync, ctx.case_id, patent_type, contact.model_dump())
    await ctx.reload_case()

    output: dict[str, Any] = {
        "intake": intake_answer.model_dump(),
        "patent_type": patent_type,
        "contact": contact.model_dump(),
    }
    ctx.state.update(output)   # 让 _card 立刻读到新类型

    system = assembler.assemble(
        INTAKE_PARTS,
        runtime_ctx={"case_card": _card(ctx), "extra": {"intake": intake_answer.model_dump()}},
    )
    user = (
        "请按本阶段「汇总」要求，把已确认的边界用 3–6 行 bullet 复述给用户。\n"
        "硬性要求：**必须写明专利类型**；用户未显式指定时写成「专利类型：发明（默认）」；"
        "联系人缺项写「待填写」；不要追问、不要说明内部类型路由与检索参数。"
    )
    recap = await build_service.stream_gen(
        ctx, tag="recap", system=system, user=user, channel="chat"
    )
    output["intake_recap"] = recap.strip()
    return output


# ---------------------------------------------------------------------------
# 步骤 2：material_scan —— 逐文件消化 + 归并 + 类型改判反问
# ---------------------------------------------------------------------------


def _material_rows_sync(case_id: str, file_ids: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """案件下已转换出 markdown 的上传件（file_ids 非空时按其过滤）。"""
    rows = db.query_all(
        "SELECT * FROM files WHERE case_id=? AND md_path IS NOT NULL ORDER BY created_at ASC",
        (case_id,),
    )
    items = [dict(r) for r in rows]
    wanted = {str(f) for f in (file_ids or []) if str(f).strip()}
    if wanted:
        picked = [r for r in items if str(r["id"]) in wanted]
        if picked:
            return picked
    return items


def _read_md_sync(path: str) -> str:
    p = Path(path)
    if not p.is_file():
        return ""
    return p.read_text(encoding="utf-8", errors="replace")


def _split_chunks(text: str, budget_tokens: int = MATERIAL_CHUNK_TOKENS) -> list[str]:
    """超长材料按段落分片（每片 ≈ budget_tokens）。"""
    if build_service.estimate_tokens(text) <= budget_tokens:
        return [text]
    limit = int(budget_tokens * build_service.CHARS_PER_TOKEN)
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for para in re.split(r"\n{2,}", text):
        if size + len(para) > limit and current:
            chunks.append("\n\n".join(current))
            current, size = [], 0
        current.append(para)
        size += len(para) + 2
    if current:
        chunks.append("\n\n".join(current))
    return chunks or [text]


def _merge_cards(file_name: str, cards: Sequence[MaterialDigest]) -> dict[str, Any]:
    """分片摘要卡的服务端归并（map-reduce 的 reduce 段）。"""
    if not cards:
        return MaterialDigest(file=file_name).model_dump()
    if len(cards) == 1:
        merged = cards[0].model_dump()
        merged["file"] = file_name
        return merged

    def _union(key: str) -> list[str]:
        out: list[str] = []
        for card in cards:
            for item in getattr(card, key):
                value = str(item).strip()
                if value and value not in out:
                    out.append(value)
        return out

    summary = " ".join(c.summary_zh.strip() for c in cards if c.summary_zh.strip())
    signals = {
        key: max(getattr(c.type_signals, key) for c in cards)
        for key in ("invention", "utility_model", "design")
    }
    return {
        "file": file_name,
        "priority": min(c.priority for c in cards if c.priority) if any(c.priority for c in cards) else 4,
        "summary_zh": summary[:1200],
        "tech_points": _union("tech_points"),
        "components": _union("components"),
        "figures_mentioned": _union("figures_mentioned"),
        "sensitive_hits": _union("sensitive_hits"),
        "type_signals": signals,
        "chunks": len(cards),
    }


def aggregate_type_signals(cards: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    """跨文件聚合类型信号（按文件取均值）。"""
    totals = {"invention": 0.0, "utility_model": 0.0, "design": 0.0}
    if not cards:
        return totals
    for card in cards:
        signals = card.get("type_signals") or {}
        for key in totals:
            try:
                totals[key] += float(signals.get(key) or 0)
            except (TypeError, ValueError):
                continue
    return {k: round(v / len(cards), 3) for k, v in totals.items()}


def type_mismatch(signals: Mapping[str, float], current: str, *, margin: float = 0.2) -> str | None:
    """材料信号是否与当前类型明显不符（不符则返回建议类型）。"""
    ranked = sorted(signals.items(), key=lambda kv: float(kv[1] or 0), reverse=True)
    if not ranked:
        return None
    top, score = ranked[0]
    if top == current or float(score or 0) <= 0:
        return None
    if float(score) - float(signals.get(current) or 0) >= margin:
        return top
    return None


_TYPE_SUGGEST_FALLBACK = (
    "当前按「发明」推进。材料更像：\n"
    "- 产品形状/构造改进（卡扣、散热布局、支架等）→ 是否改为实用新型？\n"
    "- 外观造型/图案/配色 → 是否改为外观设计？\n"
    "回复「维持发明」或指定类型即可。"
)


def type_suggest_prompt() -> str:
    """类型改判反问话术（逐字取自 intake.md 的「总结性反问」模板）。"""
    try:
        body = assets_loader.get_text("prompts/disclosure/intake")
    except KeyError:  # pragma: no cover
        return _TYPE_SUGGEST_FALLBACK
    for block in re.findall(r"```[a-zA-Z]*\r?\n(.*?)```", body, re.DOTALL):
        if "维持发明" in block:
            return block.strip()
    return _TYPE_SUGGEST_FALLBACK


async def material_scan(ctx: Ctx) -> dict[str, Any]:
    """A2 材料摄取与消化：逐文件 STRUCT 摘要卡 → 归并 → 敏感词表 + 类型信号聚合。"""
    payload = _payload(ctx)
    rows = await db.arun(_material_rows_sync, ctx.case_id, payload.get("file_ids") or [])
    cards: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        text = await db.arun(_read_md_sync, str(row["md_path"]))
        if not text.strip():
            continue
        name = str(row["orig_name"])
        chunks = _split_chunks(text)
        parsed: list[MaterialDigest] = []
        for k, chunk in enumerate(chunks, 1):
            tag = f"digest.{index}" if len(chunks) == 1 else f"digest.{index}.p{k}"
            system = assembler.assemble(
                DIGEST_PARTS,
                runtime_ctx={
                    "case_card": _card(ctx),
                    "extra": {
                        "file": name,
                        "chunk": k,
                        "chunk_total": len(chunks),
                        "intake": ctx.state.get("intake"),
                    },
                },
            )
            user = (
                f"【材料文件名】{name}"
                + (f"（第 {k}/{len(chunks)} 片，只描述本片内容）" if len(chunks) > 1 else "")
                + "\n\n【材料正文（markdown）】\n"
                + chunk
            )
            parsed.append(
                await build_service.call_struct(
                    ctx, MaterialDigest, tag=tag, system=system, user=user
                )
            )
        merged = _merge_cards(name, parsed)
        cards.append(merged)
        await ctx.emit(
            "log",
            {
                "message": (
                    f"材料《{name}》消化完成（优先级 {merged.get('priority')}，"
                    f"技术点 {len(merged.get('tech_points') or [])} 条"
                    + (f"，分片 {len(chunks)} 个" if len(chunks) > 1 else "")
                    + "）。"
                )
            },
        )

    sensitive: list[str] = []
    for card in cards:
        for hit in card.get("sensitive_hits") or []:
            value = str(hit).strip()
            if value and value not in sensitive:
                sensitive.append(value)
    signals = aggregate_type_signals(cards)

    output: dict[str, Any] = {
        "material_digest": {"files": cards, "count": len(cards)},
        "sensitive_terms": sensitive,
        "type_signals": signals,
    }
    if not cards:
        await ctx.emit(
            "log", {"message": "本案未上传可消化的材料，后续阶段将仅依据边界录入与用户补充推进。"}
        )
        return output

    current = _patent_type(ctx)
    suggested = type_mismatch(signals, current)
    if suggested:
        answer = _answer(
            await ctx.await_user(
                InteractionRequest(
                    kind="type_suggest",
                    schema={
                        "type": "object",
                        "properties": {
                            "patent_type": {
                                "type": "string",
                                "title": "专利类型",
                                "enum": list(PATENT_TYPES),
                                "enumNames": ["发明", "实用新型", "外观设计"],
                                "default": current,
                            },
                            "note": {"type": "string", "title": "补充说明（可选）"},
                        },
                    },
                    prompt=type_suggest_prompt(),
                    default={"patent_type": current, "note": ""},
                )
            )
        )
        chosen = str(answer.get("patent_type") or current)
        chosen = chosen if chosen in PATENT_TYPES else current
        output["type_suggest"] = {
            "suggested": suggested,
            "chosen": chosen,
            "signals": signals,
            "changed": chosen != current,
            "note": str(answer.get("note") or ""),
        }
        if chosen != current:
            await db.aexecute(
                "UPDATE cases SET patent_type=?, updated_at=? WHERE id=?",
                (chosen, db.now_str(), ctx.case_id),
            )
            await ctx.reload_case()
            output["patent_type"] = chosen
            intake_data = dict(ctx.state.get("intake") or {})
            if intake_data:
                intake_data["patent_type"] = chosen
                intake_data["patent_type_explicit"] = True
                output["intake"] = intake_data
            await ctx.emit(
                "log",
                {"message": f"已按用户判断把专利类型改为「{build_service.PATENT_TYPE_LABEL[chosen]}」。"},
            )
    return output


# ---------------------------------------------------------------------------
# 步骤 3：points_mining —— 专利点挖掘 [PAUSE]
# ---------------------------------------------------------------------------

POINTS_USER = (
    "请基于材料摘要卡与边界录入，先**流式输出候选专利点分析**（markdown，每个候选按"
    "「技术背景 / 创新点 / 与现有技术区别 / 可实施性」展开，3–5 个），"
    "随后在文末输出一个 ```json 围栏块，字段为 "
    '{"candidates":[{"id","title","background","innovation","distinction","feasibility","score"}],'
    '"recommended","fusion_note"}。\n'
    "工作标题须含能在后续 3.1 / 框图 / 流程 / 实施例落地的领域对象，不要选事后只能写成空泛「系统/模块」的名称。"
)

POINTS_USER_UTILITY = (
    "请基于材料摘要卡与边界录入，先**流式输出候选结构点分析**（markdown，每个候选按"
    "「技术背景 / 构造创新 / 图证 / 与现有技术区别 / 可实施性」展开，2–4 个，结构向，勿默认「方法」），"
    "随后在文末输出一个 ```json 围栏块，字段为 "
    '{"candidates":[{"id","title","background","innovation","distinction","feasibility","score"}],'
    '"recommended","fusion_note","type_mismatch_note"}。\n'
    "工作标题写成「一种……装置/结构」；候选须能回溯到部件形状、连接关系或空间布局，"
    "材料更像纯算法/纯外观时在 `type_mismatch_note` 说明改判建议。\n"
    "（本案的 StructureSchema 与 figure_plan 将在下一步「填表与线稿」中固化，"
    "此处先按材料给出的部件与连接描述挖点。）"
)

POINTS_USER_DESIGN = (
    "请基于材料摘要卡与边界录入，先**流式输出候选外观点分析**（markdown，每个候选按"
    "「产品名称与用途 / 设计要点 / 视图与图证 / 与在先外观差异」展开，1–3 个），"
    "随后在文末输出一个 ```json 围栏块，字段为 "
    '{"candidates":[{"id","title","background","innovation","distinction","feasibility","score"}],'
    '"recommended","fusion_note","type_mismatch_note"}。\n'
    "`title` 写产品名称；要点只写**看得见的**形状/线条/比例/图案/配色，"
    "材料更像内部构造、电路、受力或工艺时在 `type_mismatch_note` 说明改判建议。\n"
    "（本案的 AppearanceSchema 与 figure_plan 将在下一步「填表与线稿」中固化，"
    "此处先按材料给出的可见造型描述挖点。）"
)

POINTS_USER_BY_TYPE = {
    "invention": POINTS_USER,
    "utility_model": POINTS_USER_UTILITY,
    "design": POINTS_USER_DESIGN,
}


async def points_mining(ctx: Ctx) -> dict[str, Any]:
    """A3 专利点挖掘：GEN 流式候选分析 + 尾部 JSON → 用户勾选/融合（按类型选 prompt）。"""
    ptype = _patent_type(ctx)
    system = assembler.assemble(
        POINTS_PARTS_BY_TYPE.get(ptype, POINTS_PARTS),
        runtime_ctx={
            "case_card": _card(ctx),
            "extra": {
                "intake": ctx.state.get("intake"),
                "material_digest": (ctx.state.get("material_digest") or {}).get("files") or [],
                "sensitive_terms": ctx.state.get("sensitive_terms") or [],
                "patent_type": ptype,
            },
        },
    )
    markdown = await build_service.stream_gen(
        ctx,
        tag="points",
        system=system,
        user=POINTS_USER_BY_TYPE.get(ptype, POINTS_USER),
        channel="chat",
    )

    points: PatentPoints | None = None
    tail = _tail_json(markdown)
    if tail is not None:
        try:
            points = PatentPoints.model_validate(tail)
        except ValidationError as exc:
            logger.warning("专利点尾部 JSON 校验失败，改用抽取调用：%s", exc)
    if points is None or not points.candidates:
        points = await build_service.call_struct(
            ctx,
            PatentPoints,
            tag="points.json",
            system=system,
            user=(
                "请把下面的候选专利点分析原样结构化为 JSON（不得改写、增删技术内容）：\n\n" + markdown
            ),
        )

    candidates = [c.model_dump() for c in points.candidates]
    recommended = points.recommended or (candidates[0]["id"] if candidates else "")
    default_title = next(
        (c["title"] for c in candidates if c["id"] == recommended),
        candidates[0]["title"] if candidates else "",
    )
    answer = _answer(
        await ctx.await_user(
            InteractionRequest(
                kind="patent_points",
                schema={
                    "type": "object",
                    "properties": {
                        "selected_ids": {
                            "type": "array",
                            "title": "选定的专利点（可多选融合）",
                            "items": {
                                "type": "string",
                                "enum": [c["id"] for c in candidates],
                            },
                        },
                        "working_title": {
                            "type": "string",
                            "title": "产品名称（可修改）" if ptype == "design" else "工作标题（可修改）",
                        },
                        "instruction": {"type": "string", "title": "融合或调整说明（可选）"},
                    },
                },
                prompt=(
                    f"共产出 {len(candidates)} 个候选{'外观点' if ptype == 'design' else '专利点'}"
                    f"（{_type_label(ptype)}，建议 {POINTS_RANGE.get(ptype, '3–5')} 个），"
                    f"建议选定 {recommended or '（无）'}。"
                    "请勾选要撰写的条目（可多选表示融合），并确认名称。"
                    + (f"\n\n类型改判提示：{points.type_mismatch_note}" if points.type_mismatch_note else "")
                ),
                default={
                    "selected_ids": [recommended] if recommended else [],
                    "working_title": default_title,
                    "instruction": points.fusion_note,
                    "candidates": candidates,
                    "type_mismatch_note": points.type_mismatch_note,
                },
            )
        )
    )

    picked_ids = [str(i) for i in (answer.get("selected_ids") or []) if str(i).strip()]
    selected = [c for c in candidates if c["id"] in picked_ids]
    if not selected:
        selected = [c for c in candidates if c["id"] == recommended] or candidates[:1]
    working_title = str(answer.get("working_title") or default_title or "").strip()
    return {
        "points": {
            "candidates": candidates,
            "recommended": recommended,
            "fusion_note": points.fusion_note,
            "markdown": markdown.strip(),
        },
        "selected_points": selected,
        "working_title": working_title,
        "points_instruction": str(answer.get("instruction") or "").strip(),
    }


# ---------------------------------------------------------------------------
# 步骤 3b：schema_fill —— 事实合同填表 + 附图打分 + 线稿说明 [PAUSE]
#（§2 A3b；只有实用新型 / 外观分支含本步，发明仍是八步）
# ---------------------------------------------------------------------------

# 类型改判反问的轮数上限（改判后最多重填一次；防止反复改类型把本步转成死循环）
MAX_SCHEMA_ROUNDS = 2

SCHEMA_USER_BY_TYPE: dict[str, str] = {
    "utility_model": (
        "请把本案材料与图片中的**结构事实**填入 StructureSchema，只输出一个 JSON 围栏块。\n\n"
        "硬性要求：\n"
        "1. `parts` 逐条给件号 `id`（\"1\"、\"2\"…）、`name`（部件名）、`shape`（板状/杆状/壳体/鳍片阵列等）；"
        "材质看不出写 `unknown`；\n"
        "2. `relations` 的 `from` / `to` 写件号，`type` 取「卡扣|螺栓|铰接|焊接|一体成型|过盈|粘接|其他」，"
        "`where` 写连接位置，`seen_in` 可列出能看见该连接的图；\n"
        "3. `spatial` 写上下/内外/同轴/阵列等相对位置短句，`function_of_structure` 写该构造解决什么"
        "装配/受力/通道/拆装问题；\n"
        "4. 跨图对不上的件号、比例、连接方式，以及材料未写明的尺寸、公差、材料，一律写进 `uncertain`，"
        "**禁止杜撰**；\n"
        "5. 材料更像纯算法、纯方法或纯外观美感时，把理由写进 `not_utility_model_signals`"
        "（平台会据此反问是否改判专利类型），不要硬凑结构事实。"
    ),
    "design": (
        "请把本案材料与图片中的**外观事实**填入 AppearanceSchema，只输出一个 JSON 围栏块。\n\n"
        "硬性要求：\n"
        "1. 先判 `product_form`（`solid` 立体产品 / `planar` 平面产品），再按设计要点列出 `claimed_faces`"
        "（要点落面），**不要默认凑六视**；\n"
        "2. 相同、对称或无要点的面写进 `omitted_views`（`name` + `reason`），与「缺图」区分开；\n"
        "3. `views` 只列已见且拟用的视图（含立体图），`source_image` 填对应图片文件名；\n"
        "4. `design_points` / `ornament` / `color` 只写**看得见的**形状、线条、比例、图案与配色；"
        "看不清的色彩或纹理写进 `uncertain`，要点落面缺源图同样写进 `uncertain`；\n"
        "5. 材料更像内部构造、电路、受力或工艺步骤时，把理由写进 `not_design_signals`"
        "（平台会据此反问是否改判专利类型），不要写成外观要点。"
    ),
}

_ROLE_ENUM = list(figure_plan_service.ROLE_LABEL)
_KIND_ENUM = list(figure_plan_service.KIND_LABEL)

# figure_plan 卡片里每条图的可编辑字段（前端表单渲染依据）
_FIGURE_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "title": "图片（文件名，勿改）"},
        "fig": {"type": ["integer", "null"], "title": "图号"},
        "role": {
            "type": "string",
            "title": "角色",
            "enum": _ROLE_ENUM,
            "enumNames": [figure_plan_service.ROLE_LABEL[r] for r in _ROLE_ENUM],
        },
        "kind": {
            "type": "string",
            "title": "形态",
            "enum": _KIND_ENUM,
            "enumNames": [figure_plan_service.KIND_LABEL[k] for k in _KIND_ENUM],
        },
        "relevance": {"type": "number", "title": "相关度（0–100）"},
        "quality": {"type": "number", "title": "清晰度（0–100）"},
        "score": {"type": "number", "title": "综合分（合格线 70）"},
        "use_in_disclosure": {"type": "boolean", "title": "入文"},
        "covers": {"type": "array", "title": "覆盖对象", "items": {"type": "string"}},
        "reason": {"type": "string", "title": "取舍理由"},
    },
}

_STRUCTURE_FORM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "结构事实合同（StructureSchema）",
    "properties": {
        "parts": {
            "type": "array",
            "title": "部件表",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "title": "件号"},
                    "name": {"type": "string", "title": "名称"},
                    "shape": {"type": "string", "title": "形状要点"},
                    "material_hint": {"type": "string", "title": "材料（未知写 unknown）"},
                },
            },
        },
        "relations": {
            "type": "array",
            "title": "连接与配合",
            "items": {
                "type": "object",
                "properties": {
                    "from": {"type": "string", "title": "自（件号）"},
                    "to": {"type": "string", "title": "至（件号）"},
                    "type": {"type": "string", "title": "连接类型"},
                    "where": {"type": "string", "title": "位置"},
                },
            },
        },
        "spatial": {"type": "array", "title": "空间布局", "items": {"type": "string"}},
        "function_of_structure": {"type": "array", "title": "构造作用", "items": {"type": "string"}},
        "delta_hypothesis": {"type": "array", "title": "可能的新颖之处", "items": {"type": "string"}},
        "uncertain": {"type": "array", "title": "待确认（不得写死）", "items": {"type": "string"}},
    },
}

_APPEARANCE_FORM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "外观事实合同（AppearanceSchema）",
    "properties": {
        "product_name": {"type": "string", "title": "产品名称"},
        "overall_shape": {"type": "string", "title": "整体造型（一句话）"},
        "product_form": {
            "type": "string",
            "title": "产品形态",
            "enum": ["solid", "planar"],
            "enumNames": ["立体产品", "平面产品"],
        },
        "claimed_faces": {"type": "array", "title": "设计要点落面", "items": {"type": "string"}},
        "omitted_views": {
            "type": "array",
            "title": "故意省略的视图",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "title": "视图"},
                    "reason": {"type": "string", "title": "省略理由"},
                },
            },
        },
        "views": {
            "type": "array",
            "title": "拟入文视图",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "title": "视图名"},
                    "notes": {"type": "string", "title": "说明"},
                    "source_image": {"type": "string", "title": "对应图片"},
                },
            },
        },
        "design_points": {"type": "array", "title": "设计要点", "items": {"type": "string"}},
        "ornament": {"type": "array", "title": "图案与纹理", "items": {"type": "string"}},
        "color": {"type": "array", "title": "色彩", "items": {"type": "string"}},
        "contrast_to_prior": {"type": "array", "title": "与常见外观的差异", "items": {"type": "string"}},
        "uncertain": {"type": "array", "title": "待确认（不得写死）", "items": {"type": "string"}},
    },
}

_SCHEMA_FORM_BY_TYPE = {
    "utility_model": _STRUCTURE_FORM_SCHEMA,
    "design": _APPEARANCE_FORM_SCHEMA,
}


def schema_fill_form(patent_type: str) -> dict[str, Any]:
    """A3b 门控的表单契约：事实合同 + figure_plan 逐图入文选择 + 备注。"""
    return {
        "type": "object",
        "properties": {
            "schema": _SCHEMA_FORM_BY_TYPE.get(patent_type, _STRUCTURE_FORM_SCHEMA),
            "figure_plan": {
                "type": "object",
                "title": "附图计划（合格线 70 / CAD 永不入文）",
                "properties": {
                    "figures": {"type": "array", "title": "逐图打分与入文选择", "items": _FIGURE_ITEM_SCHEMA}
                },
            },
            "note": {"type": "string", "title": "补充说明（可选）"},
        },
    }


def _image_cards(images: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """图片清单（注入 prompt 与回显给前端；不外泄磁盘绝对路径）。"""
    cards: list[dict[str, Any]] = []
    for img in images:
        name = str(img.get("orig_name") or "")
        caption = str(img.get("caption") or "")
        cards.append(
            {
                "file_id": str(img.get("file_id") or ""),
                "path": name,
                "caption": caption,
                "hint_kind": figure_plan_service.guess_kind(name, caption),
                "exists": bool(img.get("exists", True)),
            }
        )
    return cards


def _relation_ends(rel: Mapping[str, Any]) -> tuple[str, str]:
    """连接条目的两端件号（兼容 `from` 与 pydantic 字段名 `from_`）。"""
    return str(rel.get("from") or rel.get("from_") or ""), str(rel.get("to") or "")


def schema_digest(patent_type: str, data: Mapping[str, Any]) -> dict[str, Any]:
    """事实合同摘要（注入 figure_plan 打分调用，避免整份合同重复进上下文）。"""
    if patent_type == "utility_model":
        parts = [p for p in (data.get("parts") or []) if isinstance(p, Mapping)]
        relations: list[dict[str, str]] = []
        for rel in data.get("relations") or []:
            if not isinstance(rel, Mapping):
                continue
            src, dst = _relation_ends(rel)
            relations.append({"from": src, "to": dst, "type": str(rel.get("type") or "")})
        return {
            "parts": [{"id": str(p.get("id") or ""), "name": str(p.get("name") or "")} for p in parts],
            "relations": relations,
            "spatial": [str(s) for s in (data.get("spatial") or [])],
            "uncertain": [str(u) for u in (data.get("uncertain") or [])],
        }
    views = [v for v in (data.get("views") or []) if isinstance(v, Mapping)]
    omitted = [o for o in (data.get("omitted_views") or []) if isinstance(o, Mapping)]
    return {
        "product_name": str(data.get("product_name") or ""),
        "product_form": str(data.get("product_form") or "solid"),
        "claimed_faces": [str(f) for f in (data.get("claimed_faces") or [])],
        "design_points": [str(p) for p in (data.get("design_points") or [])],
        "views": [str(v.get("name") or "") for v in views],
        "omitted_views": [str(o.get("name") or "") for o in omitted],
        "uncertain": [str(u) for u in (data.get("uncertain") or [])],
    }


def _schema_keywords(ctx: Ctx, patent_type: str, data: Mapping[str, Any]) -> list[str]:
    """附图打分的相关度关键词（标题实词 + 部件名 / 设计要点）。"""
    title = str(ctx.state.get("working_title") or (ctx.case or {}).get("title") or "")
    names = [
        str(p.get("name") or "")
        for p in (data.get("parts") or [])
        if isinstance(p, Mapping)
    ]
    return figure_plan_service.keywords_from(
        terminology.extract_title_terms(title),
        names,
        [str(x) for x in (data.get("design_points") or [])],
        [str(x) for x in (data.get("claimed_faces") or [])],
    )


def _product_name(ctx: Ctx, patent_type: str, data: Mapping[str, Any]) -> str:
    """线稿说明抬头用的产品名称。"""
    if patent_type == "design":
        name = str(data.get("product_name") or "").strip()
        if name:
            return name
    return str(ctx.state.get("working_title") or (ctx.case or {}).get("title") or "").strip()


def _lineart_goals(patent_type: str, data: Mapping[str, Any], views: Sequence[str]) -> dict[str, str]:
    """每个待补线稿位的「本图要表现什么」（写进 gen_prompt 模板）。"""
    if patent_type == "utility_model":
        names = [str(p.get("name") or "") for p in (data.get("parts") or []) if isinstance(p, Mapping)]
        goal = "、".join(n for n in names[:6] if n)
        goal = f"{goal} 的形状与连接配合关系" if goal else "部件形状与连接配合关系"
    else:
        points = [str(p) for p in (data.get("design_points") or []) if str(p).strip()]
        goal = "；".join(points[:3]) or "整体造型与可见装饰线条"
    return {str(v): goal for v in views}


async def _fill_schema(
    ctx: Ctx,
    patent_type: str,
    model_cls: Any,
    images: Sequence[Mapping[str, Any]],
    *,
    tag: str,
):
    """A3b 前半：STRUCT 产事实合同（StructureSchema / AppearanceSchema）。"""
    cards = _image_cards(images)
    degraded = not figure_plan_service.vision_available()
    system = assembler.assemble(
        SCHEMA_FILL_PARTS[patent_type],
        runtime_ctx={
            "case_card": _card(ctx),
            "extra": {
                "intake": ctx.state.get("intake"),
                "selected_points": ctx.state.get("selected_points") or [],
                "working_title": ctx.state.get("working_title") or "",
                "material_digest": (ctx.state.get("material_digest") or {}).get("files") or [],
                "sensitive_terms": ctx.state.get("sensitive_terms") or [],
                "images": cards,
                "vision_enabled": not degraded,
            },
        },
    )
    lines = [SCHEMA_USER_BY_TYPE[patent_type]]
    if degraded:
        lines += [
            "",
            (
                "**注意：本次调用未启用多模态识图**（模型配置无 vision 能力位）。请**仅依据材料文本、"
                "文件名与图注**填写，看不出的一律进 `uncertain`，**不要臆造图面内容**"
                "（用户可在随后的卡片里逐项修改）。"
            ),
        ]
    lines += [
        "",
        "【已选定的专利点】",
        build_service.json_block(ctx.state.get("selected_points") or []),
        "",
        "【材料摘要卡】",
        build_service.json_block((ctx.state.get("material_digest") or {}).get("files") or []),
        "",
        "【本案图片清单】",
        build_service.json_block(cards) if cards else "（本案未上传图片）",
    ]
    return await build_service.call_struct(
        ctx, model_cls, tag=tag, system=system, user="\n".join(lines)
    )


def _rejudge_prompt(patent_type: str, signals: Sequence[str]) -> str:
    """类型改判反问话术（口径同 A2b：给出信号，让用户裁决，不擅自改类型）。"""
    label = _type_label(patent_type)
    return (
        f"填表时发现材料中存在与「{label}」不符的信号：\n"
        + "\n".join(f"- {s}" for s in signals[:5])
        + f"\n\n是否改判专利类型？维持「{label}」可直接确认；"
        "改判后平台会按新类型重新填一次事实合同。"
    )


async def _ask_type_rejudge(ctx: Ctx, patent_type: str, signals: Sequence[str]) -> tuple[str, str]:
    """not_*_signals 非空时的改判反问 [PAUSE]；返回 (选定类型, 备注)。"""
    answer = _answer(
        await ctx.await_user(
            InteractionRequest(
                kind="type_suggest",
                schema={
                    "type": "object",
                    "properties": {
                        "patent_type": {
                            "type": "string",
                            "title": "专利类型",
                            "enum": list(PATENT_TYPES),
                            "enumNames": ["发明", "实用新型", "外观设计"],
                            "default": patent_type,
                        },
                        "note": {"type": "string", "title": "补充说明（可选）"},
                    },
                },
                prompt=_rejudge_prompt(patent_type, signals),
                default={"patent_type": patent_type, "note": "", "signals": list(signals)},
            )
        )
    )
    chosen = str(answer.get("patent_type") or patent_type)
    return (chosen if chosen in PATENT_TYPES else patent_type), str(answer.get("note") or "")


async def _switch_patent_type(ctx: Ctx, chosen: str) -> None:
    """把用户裁决的新类型写回案件行并刷新上下文。"""
    await db.aexecute(
        "UPDATE cases SET patent_type=?, updated_at=? WHERE id=?",
        (chosen, db.now_str(), ctx.case_id),
    )
    await ctx.reload_case()
    ctx.state["patent_type"] = chosen
    await ctx.emit(
        "log",
        {"message": f"已按用户判断把专利类型改为「{build_service.PATENT_TYPE_LABEL[chosen]}」。"},
    )


def _schema_fill_prompt(
    patent_type: str,
    plan: Mapping[str, Any],
    brief: Mapping[str, Any],
    warnings: Sequence[str],
) -> str:
    """A3b 门控话术：事实合同回显 + 入文附图结论 + 线稿绘制说明。"""
    used = figure_plan_service.used_figures(plan)
    label = _type_label(patent_type)
    contract = "结构事实合同" if patent_type == "utility_model" else "外观事实合同"
    lines = [
        (
            f"已按材料与图片填好本案的{contract}（{label}），并对上传图片逐张打分。"
            "请核对下方表单：部件/要点、连接或视图、待确认项都可直接修改，附图可勾选是否入文。"
        ),
        "",
        (
            f"入文附图 {len(used)} 幅："
            + "、".join(figure_plan_service.figure_caption(f) for f in used)
            if used
            else "当前没有满足入文条件的附图（合格线 70 分；CAD 永不入文；"
            + ("实用新型入文只收线稿）。" if patent_type == "utility_model" else "外观入文只收干净实拍与线稿）。")
        ),
    ]
    if warnings:
        lines += ["", "平台提示：", *[f"- {w}" for w in warnings[:6]]]
    if brief.get("needed"):
        lines += ["", "【线稿绘制说明】本期不做线稿 AI 生成，请按下列说明线下绘制或用绘图工具产出后上传："]
        for view in brief.get("views") or []:
            lines.append(f"- {view.get('view_name')}：{view.get('gen_prompt')}")
        forbid = [str(f) for f in (brief.get("forbid") or [])]
        if forbid:
            lines.append("- 统一禁止：" + "；".join(forbid))
        if brief.get("risk_note"):
            lines += ["", str(brief["risk_note"])]
    return "\n".join(lines)


async def schema_fill(ctx: Ctx) -> dict[str, Any]:
    """A3b 填表与线稿（实用新型 / 外观）：事实合同 + figure_plan + 线稿说明 [PAUSE]。

    - STRUCT 产 `StructureSchema` / `AppearanceSchema`；`not_*_signals` 非空先走类型改判反问；
    - `services/figure_plan` 逐图打分（CAD 永不入文；合格线 70；实用只收线稿、外观实拍+线稿都入文）；
    - 缺入文线稿时把「线稿绘制说明」（gen_prompt 模板）随卡片下发，外观仅有实拍时提示风险但允许继续；
    - 门控回填允许用户修改事实合同与逐图入文选择，覆盖后落进 `cases.state_json`。
    """
    ptype = _patent_type(ctx)
    if ptype not in SCHEMA_SPEC:  # 发明分支不含本步；防御性直通
        return {}

    images = await figure_plan_service.load_case_images(ctx.case_id)
    output: dict[str, Any] = {}
    type_suggest: dict[str, Any] | None = None
    schema_data: dict[str, Any] = {}

    rounds = 0
    while True:
        rounds += 1
        _key, model_cls, signal_field = SCHEMA_SPEC[ptype]
        schema_obj = await _fill_schema(
            ctx,
            ptype,
            model_cls,
            images,
            tag="schema" if rounds == 1 else f"schema.round{rounds}",
        )
        schema_data = schema_obj.model_dump(by_alias=True)
        signals = [
            str(s).strip() for s in (getattr(schema_obj, signal_field, None) or []) if str(s).strip()
        ]
        if not signals or rounds >= MAX_SCHEMA_ROUNDS:
            if signals:
                await ctx.emit(
                    "log",
                    {
                        "message": (
                            f"事实合同仍带 {len(signals)} 条类型不符信号，已达改判反问轮次上限，"
                            f"按「{_type_label(ptype)}」继续推进。"
                        )
                    },
                )
            break
        chosen, note = await _ask_type_rejudge(ctx, ptype, signals)
        type_suggest = {
            "from": ptype,
            "chosen": chosen,
            "changed": chosen != ptype,
            "field": signal_field,
            "signals": signals,
            "note": note,
        }
        if chosen == ptype:
            break
        await _switch_patent_type(ctx, chosen)
        output["patent_type"] = chosen
        intake_data = dict(ctx.state.get("intake") or {})
        if intake_data:
            intake_data["patent_type"] = chosen
            intake_data["patent_type_explicit"] = True
            output["intake"] = intake_data
        if chosen not in SCHEMA_SPEC:
            # 改判为发明：发明分支不需要事实合同与附图计划，本步就此收口
            await ctx.emit(
                "log",
                {"message": "已改判为发明，本步不再产出事实合同与附图计划，后续按发明主线推进。"},
            )
            output["schema_fill"] = {
                "patent_type": chosen,
                "skipped": True,
                "type_suggest": type_suggest,
            }
            return output
        ptype = chosen

    state_key = SCHEMA_SPEC[ptype][0]
    plan, report = await figure_plan_service.plan_figures(
        ctx,
        patent_type=ptype,
        images=images,
        theme=str(ctx.state.get("working_title") or (ctx.case or {}).get("title") or ""),
        schema_digest=schema_digest(ptype, schema_data),
        keywords=_schema_keywords(ctx, ptype, schema_data),
        tag="figure_plan",
    )

    def _brief(current_plan: Mapping[str, Any]) -> dict[str, Any]:
        gaps = figure_plan_service.lineart_gaps(current_plan, ptype)
        return figure_plan_service.lineart_brief(
            ptype,
            plan=current_plan,
            product_name=_product_name(ctx, ptype, schema_data),
            goals=_lineart_goals(ptype, schema_data, gaps),
            parts_legend=[
                {"id": str(p.get("id") or ""), "name": str(p.get("name") or "")}
                for p in (schema_data.get("parts") or [])
                if isinstance(p, Mapping)
            ],
        )

    brief = _brief(plan)
    warnings = figure_plan_service.plan_warnings(plan, report, ptype)

    answer = _answer(
        await ctx.await_user(
            InteractionRequest(
                kind="schema_fill",
                schema=schema_fill_form(ptype),
                prompt=_schema_fill_prompt(ptype, plan, brief, warnings),
                default={
                    "schema": schema_data,
                    "figure_plan": plan,
                    "note": "",
                    "patent_type": ptype,
                    "figure_report": report,
                    "warnings": warnings,
                    "lineart_brief": brief,
                    "images": _image_cards(images),
                },
            )
        )
    )

    edited = False
    user_schema = answer.get("schema")
    if isinstance(user_schema, Mapping):
        model_cls = SCHEMA_SPEC[ptype][1]
        try:
            schema_data = model_cls.model_validate(dict(user_schema)).model_dump(by_alias=True)
            edited = True
        except ValidationError as exc:
            await ctx.emit(
                "log", {"message": f"用户回填的事实合同未通过契约校验，已沿用平台版本：{exc.error_count()} 处"}
            )
    if answer.get("figure_plan"):
        plan, report = figure_plan_service.apply_user_plan(
            plan, answer["figure_plan"], ptype, images
        )
        edited = True
    if edited:
        brief = _brief(plan)
        warnings = figure_plan_service.plan_warnings(plan, report, ptype)

    used = figure_plan_service.used_figures(plan)
    await ctx.emit(
        "log",
        {
            "message": (
                f"填表与线稿完成（{_type_label(ptype)}）：事实合同已固化，"
                f"候选图 {len(images)} 张、入文 {len(used)} 幅"
                + ("；仍缺合格线稿，已下发线稿绘制说明。" if brief.get("needed") else "。")
            )
        },
    )

    output[state_key] = schema_data
    output["figure_plan"] = plan
    output["schema_fill"] = {
        "patent_type": ptype,
        "skipped": False,
        "state_key": state_key,
        "rounds": rounds,
        "edited": edited,
        "figure_report": report,
        "warnings": warnings,
        "lineart_brief": brief,
        "images": _image_cards(images),
        "used_figures": len(used),
        "note": str(answer.get("note") or ""),
        "type_suggest": type_suggest,
    }
    return output


# ---------------------------------------------------------------------------
# 步骤 4：prior_art_search —— 联网查新 [PAUSE]
# ---------------------------------------------------------------------------

# 检索词泛词表（search_terms.md：不得为单字或「系统」「方法」类泛词）
GENERIC_TERMS = {
    "系统", "方法", "装置", "设备", "平台", "检索", "增强", "技术", "数据", "模型",
    "流程", "模块", "应用", "处理", "控制", "信息", "管理", "服务", "优化", "智能",
}
MIN_BLOCK_CHARS = 2
MAX_BLOCK_CHARS = 16


def lint_search_blocks(blocks: Sequence[str]) -> list[str]:
    """检索单位服务端校验（块数 2–8、块长、泛词、整句长串）。"""
    problems: list[str] = []
    items = [str(b).strip() for b in blocks if str(b).strip()]
    if not 2 <= len(items) <= 8:
        problems.append(f"检索单位共 {len(items)} 个，须为 2–8 个")
    seen: set[str] = set()
    for block in items:
        if block in seen:
            problems.append(f"检索单位「{block}」重复")
            continue
        seen.add(block)
        if len(block) < MIN_BLOCK_CHARS:
            problems.append(f"检索单位「{block}」过短（单字无检索意义）")
        elif len(block) > MAX_BLOCK_CHARS:
            problems.append(
                f"检索单位「{block}」过长（{len(block)} 字）：无空格的整句长串在公布站单框易被当整句 AND，"
                "请拆成 2–12 字的语义块"
            )
        if block in GENERIC_TERMS:
            problems.append(f"检索单位「{block}」是泛词，须换成专业术语/名词短语/名动组合")
        if re.search(r"\s", block):
            problems.append(f"检索单位「{block}」含空格，请拆成独立的语义块")
    return problems


async def _search_terms(ctx: Ctx) -> tuple[list[str], str, dict[str, Any]]:
    """A4 前半：STRUCT 构造检索单位 + 服务端校验（违规 REPAIR ≤2）。"""
    ptype = _patent_type(ctx)
    system = assembler.assemble(
        SEARCH_TERMS_PARTS,
        runtime_ctx={
            "case_card": _card(ctx),
            "extra": {
                "selected_points": ctx.state.get("selected_points") or [],
                "working_title": ctx.state.get("working_title") or "",
                "material_digest": (ctx.state.get("material_digest") or {}).get("files") or [],
                "patent_type": ptype,
            },
        },
    )
    user = (
        "请为本案构造国知局公布公告站的检索单位，只输出一个 JSON 围栏块。"
        f"`type_param` 取「{ptype}」。"
    )
    plan: SearchTermsPlan = await build_service.call_struct(
        ctx, SearchTermsPlan, tag="terms", system=system, user=user
    )
    problems = lint_search_blocks(plan.blocks)
    repairs = 0
    while problems and repairs < MAX_TERMS_REPAIR:
        repairs += 1
        await ctx.emit(
            "log", {"message": f"检索词未通过服务端校验（第 {repairs} 次修复）：{'；'.join(problems[:3])}"}
        )
        plan = await build_service.call_struct(
            ctx,
            SearchTermsPlan,
            tag=f"terms.repair{repairs}",
            system=system,
            user=(
                "上一版检索单位未通过服务端校验，问题如下：\n"
                + "\n".join(f"- {p}" for p in problems)
                + "\n\n请修正后重新输出完整 JSON。\n\n"
                + user
            ),
            call_class="REPAIR",
        )
        problems = lint_search_blocks(plan.blocks)
    blocks = [str(b).strip() for b in plan.blocks if str(b).strip()]
    type_param = str(plan.type_param or ptype)
    return blocks, type_param, {"repairs": repairs, "unresolved": problems, "rationale": plan.rationale}


def _hit_payload(hit: Any) -> dict[str, Any]:
    """命中条目 → 注入 abstract_digest 的字段（键名照 prompt 契约）。"""
    return {
        "pub_number": getattr(hit, "pub_no", None) or "",
        "title": getattr(hit, "title", None) or "",
        "applicant": getattr(hit, "applicant", None) or "",
        "pub_date": getattr(hit, "pub_date", None) or "",
        "link": getattr(hit, "url", "") or "",
        "abstract": getattr(hit, "abstract", None) or "",
    }


async def _digest_hits(ctx: Ctx, hits: Sequence[Any]) -> list[dict[str, Any]]:
    """A4 后半：分批（≤8 条）消化改写；URL 必须照抄 link（白名单 lint + REPAIR）。"""
    allowed = {str(getattr(h, "url", "") or "").strip() for h in hits}
    allowed.discard("")
    notes: list[dict[str, Any]] = []
    for bi in range(0, len(hits), DIGEST_BATCH):
        batch = list(hits[bi : bi + DIGEST_BATCH])
        tag = f"digest.batch{bi // DIGEST_BATCH + 1}"
        system = assembler.assemble(
            ABSTRACT_DIGEST_PARTS,
            runtime_ctx={
                "case_card": _card(ctx),
                "extra": {
                    "selected_points": ctx.state.get("selected_points") or [],
                    "working_title": ctx.state.get("working_title") or "",
                },
            },
        )
        user = (
            "本批命中条目如下（逐条消化改写，顺序与注入一致；`url` 只准照抄该条 `link` 字段）：\n\n"
            + build_service.json_block([_hit_payload(h) for h in batch])
        )
        result: AbstractDigests = await build_service.call_struct(
            ctx, AbstractDigests, tag=tag, system=system, user=user
        )
        items = [d.model_dump() for d in result.digests]
        repairs = 0
        problems = build_service.url_lint(
            "\n".join(str(i.get("url") or "") for i in items), allowed
        )
        while problems and repairs < MAX_DIGEST_REPAIR:
            repairs += 1
            await ctx.emit(
                "log",
                {"message": f"查新笔记的来源链接未通过白名单校验（第 {repairs} 次修复）。"},
            )
            result = await build_service.call_struct(
                ctx,
                AbstractDigests,
                tag=f"{tag}.repair{repairs}",
                system=system,
                user=(
                    "上一版查新笔记的 `url` 未照抄命中条目的 `link` 字段，问题如下：\n"
                    + "\n".join(f"- {p}" for p in problems)
                    + "\n\n请重新输出，`url` 逐字照抄对应条目的 `link`。\n\n"
                    + user
                ),
                call_class="REPAIR",
            )
            items = [d.model_dump() for d in result.digests]
            problems = build_service.url_lint(
                "\n".join(str(i.get("url") or "") for i in items), allowed
            )
        kept = [i for i in items if str(i.get("url") or "").strip() in allowed]
        dropped = len(items) - len(kept)
        if dropped:
            await ctx.emit(
                "log",
                {"message": f"{dropped} 条查新笔记因来源链接不在命中集合内被丢弃（禁止编造 URL）。"},
            )
        notes.extend(kept)

    by_url = {str(getattr(h, "url", "") or ""): h for h in hits}
    for note in notes:
        hit = by_url.get(str(note.get("url") or ""))
        if hit is None:
            continue
        note["hit_id"] = getattr(hit, "id", "")
        digest_text = "；".join(
            x for x in (note.get("scheme_summary"), note.get("application"), note.get("limitation")) if x
        )
        try:
            await cnipa.set_digest(str(getattr(hit, "id", "")), digest_text)
        except Exception as exc:  # noqa: BLE001 —— 命中行可能来自缓存/内存，记账失败不阻断
            logger.debug("写回 search_hits.digest 失败：%s", exc)
    return notes


SEARCH_FAIL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "title": "处置方式",
            "enum": ["retry", "manual", "skip"],
            "enumNames": ["重试检索", "手工粘贴在先文献", "跳过查新"],
        },
        "terms": {"type": "array", "title": "重试用检索词（可留空沿用）", "items": {"type": "string"}},
        "hits": {
            "type": "array",
            "title": "手工录入的在先文献（url 必填）",
            "items": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "title": "公开源 URL"},
                    "pub_no": {"type": "string", "title": "公开号"},
                    "title": {"type": "string", "title": "标题"},
                    "applicant": {"type": "string", "title": "申请方"},
                    "abstract": {"type": "string", "title": "摘要（可选）"},
                },
                "required": ["url"],
            },
        },
        "reason": {"type": "string", "title": "跳过原因（会体现在 1.1 检索说明）"},
    },
}


async def prior_art_search(ctx: Ctx) -> dict[str, Any]:
    """A4 联网查新：检索词 → 爬虫 → 失败三选项门控 → 消化改写 → 勾选纳入。"""
    # 技能开关必须在这里生效，不能只做成界面上的一个按钮。
    #
    # 联网查新会带着本案的技术要点去访问国知局公布公告系统——用户把它关掉，
    # 通常正是出于保密考虑。如果开关只改数据库、流程照跑，那就是在用户明确
    # 收到「已停用」之后仍然把交底内容发了出去。这比功能不生效严重得多。
    if not skills_service.is_user_enabled("cnipa_search"):
        await ctx.emit(
            "log",
            {"message": "联网查新已在技能库中关闭，本案不联网检索，1.1 将如实写明。"},
        )
        # 形状必须与正常路径一致：下游按 prior_art.* 取值，缺字段会在成文时才炸
        return {
            "prior_art": {
                "searched": False,
                "status": "skipped",
                "error": None,
                "terms": [],
                "type_param": "",
                "terms_report": {},
                "rounds": 0,
                "cached": False,
                "manual": False,
                "skipped": True,
                "skip_reason": "技能库中已关闭联网查新",
                "hit_count": 0,
                "selected_count": 0,
            },
            "prior_art_notes": [],
        }

    blocks, type_param, terms_report = await _search_terms(ctx)
    progress = cnipa.hub_progress(ctx.case_id, ctx.step_key)

    result = await cnipa.search(ctx.case_id, blocks, type_param, on_progress=progress)
    hits = list(result.hits)
    status, error = result.status, result.error
    cached = bool(result.cached)
    rounds = 1
    skipped = False
    skip_reason = ""
    manual = False

    while not (result.ok and hits) and not skipped and rounds <= MAX_SEARCH_ROUNDS:
        answer = _answer(
            await ctx.await_user(
                InteractionRequest(
                    kind="prior_art",
                    schema=SEARCH_FAIL_SCHEMA,
                    prompt=(
                        "本次国知局检索未取得可用结果"
                        + (f"（{error}）" if error else "（零命中）")
                        + "。请选择：重试检索 / 手工粘贴在先文献 / 跳过查新"
                        "（跳过时 1.1 会如实写明未进行系统性检索，平台不会编造检索结果）。"
                    ),
                    default={"action": "retry", "terms": blocks, "hits": [], "reason": ""},
                )
            )
        )
        action = str(answer.get("action") or "skip")
        if action == "retry" and rounds < MAX_SEARCH_ROUNDS:
            rounds += 1
            retry_terms = [str(t).strip() for t in (answer.get("terms") or []) if str(t).strip()]
            blocks = retry_terms or blocks
            result = await cnipa.search(
                ctx.case_id, blocks, type_param, on_progress=progress, use_cache=False
            )
            hits = list(result.hits)
            status, error, cached = result.status, result.error, bool(result.cached)
            continue
        if action == "manual":
            raw_hits = [h for h in (answer.get("hits") or []) if isinstance(h, Mapping)]
            try:
                added = await cnipa.add_manual_hits(ctx.case_id, raw_hits, note="用户手工录入的在先文献")
            except ValueError as exc:
                await ctx.emit("log", {"message": f"手工录入失败：{exc}"})
                added = []
            if added:
                hits = list(added)
                status, error, manual = "done", None, True
                break
            rounds += 1
            continue
        skip_reason = str(answer.get("reason") or "").strip()
        if action == "retry":     # 重试次数已达上限：按跳过收口，避免门控死循环
            await ctx.emit(
                "log",
                {"message": f"检索重试已达上限（{MAX_SEARCH_ROUNDS} 轮），按「跳过查新」收口。"},
            )
            skip_reason = skip_reason or "检索重试已达上限，本次未取得可用的检索结果"
        await cnipa.skip_search(ctx.case_id, skip_reason)
        skipped, hits, status = True, [], "manual_pending"
        break

    notes: list[dict[str, Any]] = []
    selected_count = 0
    if hits:
        notes = await _digest_hits(ctx, hits)
        answer = _answer(
            await ctx.await_user(
                InteractionRequest(
                    kind="prior_art",
                    schema={
                        "type": "object",
                        "properties": {
                            "selected_ids": {
                                "type": "array",
                                "title": "纳入 1.1 的命中条目",
                                "items": {"type": "string", "enum": [str(h.id) for h in hits]},
                            }
                        },
                    },
                    prompt=(
                        f"共取得 {len(hits)} 条命中并完成消化改写，请勾选要写进 1.1 现有技术的条目"
                        "（默认全选；未勾选的条目仍留档在查新记录中）。"
                    ),
                    default={
                        "selected_ids": [str(h.id) for h in hits],
                        "hits": [
                            {
                                "id": str(h.id),
                                "pub_no": h.pub_no,
                                "title": h.title,
                                "applicant": h.applicant,
                                "url": h.url,
                            }
                            for h in hits
                        ],
                    },
                )
            )
        )
        if "selected_ids" in answer:
            keep = {str(i) for i in (answer.get("selected_ids") or [])}
            for hit in hits:
                try:
                    await cnipa.set_selected(str(hit.id), str(hit.id) in keep)
                except Exception as exc:  # noqa: BLE001 —— 内存态命中（缓存复用）无对应行
                    logger.debug("写回 search_hits.selected 失败：%s", exc)
            urls = {str(h.url) for h in hits if str(h.id) in keep}
            notes = [n for n in notes if str(n.get("url") or "") in urls]
        selected_count = len(notes)

    output = {
        "prior_art": {
            "searched": bool(notes) and not skipped,
            "status": status,
            "error": error,
            "terms": blocks,
            "type_param": type_param,
            "terms_report": terms_report,
            "rounds": rounds,
            "cached": cached,
            "manual": manual,
            "skipped": skipped,
            "skip_reason": skip_reason,
            "hit_count": len(hits),
            "selected_count": selected_count,
        },
        "prior_art_notes": notes,
    }
    await ctx.emit(
        "log",
        {
            "message": (
                f"查新完成：命中 {len(hits)} 条，纳入 1.1 共 {selected_count} 条。"
                if not skipped
                else "已按用户选择跳过查新，1.1 将如实写明未进行系统性专利检索。"
            )
        },
    )
    return output


# ---------------------------------------------------------------------------
# 步骤 5：preview —— 摘要预览 [PAUSE]
# ---------------------------------------------------------------------------

PREVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "title": "处置",
            "enum": ["confirm", "adjust", "skip"],
            "enumNames": ["确认，开始成文", "调整方向（带反馈重跑）", "跳过预览直接成文"],
            "default": "confirm",
        },
        "feedback": {"type": "string", "title": "调整方向的反馈（选择「调整方向」时必填）"},
    },
}


async def preview(ctx: Ctx) -> dict[str, Any]:
    """A5 摘要预览：GEN 结构化摘要 → 确认 / 调整方向（带反馈重跑）/ 跳过。"""
    feedback = ""
    markdown = ""
    action = "confirm"
    rounds = 0
    prior = dict(ctx.state.get("prior_art") or {})
    while rounds < MAX_PREVIEW_ROUNDS:
        rounds += 1
        system = assembler.assemble(
            PREVIEW_PARTS,
            runtime_ctx={
                "case_card": _card(ctx),
                "extra": {
                    "selected_points": ctx.state.get("selected_points") or [],
                    "working_title": ctx.state.get("working_title") or "",
                    "prior_art_notes": ctx.state.get("prior_art_notes") or [],
                    "searched": bool(prior.get("searched")),
                    "material_digest": (ctx.state.get("material_digest") or {}).get("files") or [],
                    "user_feedback": feedback,
                },
            },
        )
        user = (
            "请按「摘要应包含」的条目（发明）输出结构化摘要：选定名称 / 专利类型 / 解决的技术问题 1–3 条 / "
            "核心创新模块或步骤 3–6 条 / 与最相近现有技术的区别（未检索则标明「待查新」）。"
            "markdown 小节标题清晰、逐条成 bullet，总长控制在一屏内；不要在末尾追加确认问句。"
        )
        if feedback:
            user += f"\n\n用户对上一版摘要的调整意见（须落实）：{feedback}"
        markdown = await build_service.stream_gen(
            ctx,
            tag="preview" if rounds == 1 else f"preview.round{rounds}",
            system=system,
            user=user,
            channel="chat",
        )
        answer = _answer(
            await ctx.await_user(
                InteractionRequest(
                    kind="preview_confirm",
                    schema=PREVIEW_SCHEMA,
                    prompt="以上为成文前的结构化摘要，请确认方向；如需调整请填写反馈，平台会带反馈重跑本步。",
                    default={"action": "confirm", "feedback": ""},
                )
            )
        )
        action = str(answer.get("action") or "confirm")
        new_feedback = str(answer.get("feedback") or "").strip()
        if action == "adjust" and new_feedback and rounds < MAX_PREVIEW_ROUNDS:
            feedback = new_feedback
            continue
        break
    return {
        "preview": {
            "markdown": markdown.strip(),
            "action": action,
            "rounds": rounds,
            "feedback": feedback,
        }
    }


# ---------------------------------------------------------------------------
# 步骤 6：build —— 分章成文（按类型分派）
# ---------------------------------------------------------------------------


async def build(ctx: Ctx) -> dict[str, Any]:
    """A6 成文：按专利类型分派——发明 G0–G7、实用新型结构主线、外观视图主线。"""
    return await build_service.build_document(ctx, _patent_type(ctx))


# ---------------------------------------------------------------------------
# 步骤 7：self_check —— 组装 + AUDIT 补丁 + 确定性 lint 束
# ---------------------------------------------------------------------------

_FORMULA_PATCH_HINT = ("8.2", "公式", "符号", "latex", "LaTeX", "量纲")
_TERM_PATCH_HINT = ("术语", "标题贯穿", "8.1", "同词")


def _patch_kinds(report: SelfCheckReport) -> set[str]:
    kinds: set[str] = set()
    for patch in report.patches:
        item = f"{patch.checklist_item} {patch.reason}"
        if any(h in item for h in _FORMULA_PATCH_HINT):
            kinds.add("formula")
        if any(h in item for h in _TERM_PATCH_HINT):
            kinds.add("terminology")
    return kinds


def _affected_chapters(chapters: Mapping[str, str], drops: Sequence[Mapping[str, Any]]) -> list[str]:
    """按落空补丁的原文片段反查受影响章节。"""
    keys: list[str] = []
    for drop in drops:
        excerpt = str((drop.get("patch") or {}).get("original_excerpt") or "").strip()
        anchor = str((drop.get("patch") or {}).get("anchor_before") or "").strip()
        for key, text in chapters.items():
            probe = excerpt or anchor
            if probe and probe[:20] in text and key not in keys:
                keys.append(key)
    return keys


async def self_check(ctx: Ctx) -> dict[str, Any]:
    """A7 组装与自检：全文 AUDIT → 补丁应用 → 联动复核 → 确定性 lint 束（按类型选检查清单）。"""
    ptype = _patent_type(ctx)
    chapters = dict(ctx.state.get("chapters") or {})
    case_title = str(ctx.state.get("case_title") or (ctx.case or {}).get("title") or "")
    contact = ctx.state.get("contact")
    text = str(ctx.state.get("draft_markdown") or "")
    if not text.strip():
        text = build_service.assemble_document(
            chapters, case_title=case_title, contact=contact, patent_type=ptype
        )
    sheet = ctx.state.get("terminology_sheet")
    plan = ctx.state.get("formula_plan")

    rounds: list[dict[str, Any]] = []
    unresolved: list[str] = []
    term_replacements: list[dict[str, Any]] = []
    formula_recheck: dict[str, Any] | None = None
    rewritten: list[str] = []

    for round_no in range(1, MAX_SELF_CHECK_ROUNDS + 1):
        await ctx.emit(
            "log",
            {
                "message": f"自检第 {round_no} 轮：全文进审校，按检查清单产出修订补丁。",
                "kind": "self_check",
                "round": round_no,
            },
        )
        system = assembler.assemble(
            SELF_CHECK_PARTS_BY_TYPE.get(ptype, SELF_CHECK_PARTS),
            runtime_ctx={
                "case_card": _card(ctx),
                "terminology_sheet": terminology.to_prompt_dict(sheet),
                "outline": ctx.state.get("skeleton"),
                "extra": {
                    "formula_plan": plan,
                    "formula_gate": ctx.state.get("formula_gate"),
                    "prior_art_notes": ctx.state.get("prior_art_notes") or [],
                },
            },
        )
        report: SelfCheckReport = await build_service.call_struct(
            ctx,
            SelfCheckReport,
            tag="audit" if round_no == 1 else f"audit{round_no}",
            system=system,
            user=(
                "以下为组装后的交底书全文，请按检查清单逐项核对并输出修订补丁 JSON"
                "（不要输出修订后全文，也不要输出审校报告正文）。\n\n"
                "===== 交底书全文 =====\n" + text
            ),
            call_class="AUDIT",
        )
        result = patches_service.apply_patch_report(text, report)
        new_text = result["text"]
        kinds = _patch_kinds(report)

        if "terminology" in kinds:
            new_text, replaced = build_service.apply_term_family(new_text, sheet)
            term_replacements.extend(replaced)
        if "formula" in kinds and plan:
            formula_recheck = await formula_service.avalidate_formula_plan(plan)
            if formula_recheck.get("errors") and not formula_recheck.get("degraded"):
                unresolved.append(
                    "公式类补丁后门禁复跑仍有问题：" + "；".join(formula_recheck["errors"][:3])
                )

        rewrite_keys: list[str] = []
        if result["low_rate"]:
            rewrite_keys = _affected_chapters(chapters, result["dropped"])
            for key in rewrite_keys:
                problems = [
                    str(d.get("reason_zh") or d.get("reason"))
                    + "：" + str((d.get("patch") or {}).get("reason") or "")
                    for d in result["dropped"]
                ]
                runtime = {
                    "case_card": _card(ctx),
                    "terminology_sheet": terminology.to_prompt_dict(sheet),
                    "outline": ctx.state.get("skeleton"),
                    "extra": {"formula_plan": plan},
                }
                chapters[key] = await build_service.rewrite_chapter(
                    ctx,
                    key=key,
                    text=chapters[key],
                    runtime=runtime,
                    problems=problems,
                    tag=f"rewrite.{key}.r{round_no}",
                )
                rewritten.append(key)
            if rewrite_keys:
                new_text = build_service.assemble_document(
                    chapters, case_title=case_title, contact=contact, patent_type=ptype
                )

        text = new_text
        unresolved.extend(result["unresolved"])
        rounds.append(
            {
                "round": round_no,
                "verdict": result.get("verdict"),
                "applied": result["applied"],
                "dropped": len(result["dropped"]),
                "rate": result["rate"],
                "low_rate": result["low_rate"],
                "rewritten": rewrite_keys,
                "kinds": sorted(kinds),
            }
        )
        await ctx.emit(
            "log",
            {
                "message": (
                    f"自检第 {round_no} 轮：应用补丁 {result['applied']}/{result['total']} 条"
                    f"（应用率 {result['rate']:.0%}）"
                    + (f"，整章重写 {len(rewrite_keys)} 节" if rewrite_keys else "")
                    + "。"
                ),
                "kind": "self_check",
                "round": round_no,
            },
        )
        if report.verdict == "pass" and not result["dropped"]:
            break

    # ---- 确定性 lint 束（元信息泄漏 / mermaid 可渲染 / 文末清洁） ----
    text, tail_removed = build_service.clean_tail(text)
    meta_hits = build_service.meta_leak_lint(text)
    meta_fixed: list[dict[str, Any]] = []
    if meta_hits:
        text, meta_fixed = await _repair_meta_leaks(ctx, text, meta_hits)
    text, degraded = await build_service.degrade_mermaid(text)
    text = text.rstrip() + "\n"

    remaining_meta = build_service.meta_leak_lint(text)
    if remaining_meta:
        unresolved.append(
            "正文仍残留元信息词：" + "、".join(sorted({h["word"] for h in remaining_meta}))
        )

    await ctx.emit(
        "log",
        {
            "message": (
                f"自检完成：共 {len(rounds)} 轮；文末清理 {len(tail_removed)} 行；"
                f"元信息剥离 {len(meta_fixed)} 处；mermaid 降级 {degraded} 处。"
            ),
            "kind": "self_check",
        },
    )
    return {
        "chapters": chapters,
        "final_markdown": text,
        "self_check": {
            "rounds": rounds,
            "unresolved": unresolved,
            "term_replacements": term_replacements,
            "formula_recheck": formula_recheck,
            "tail_removed": tail_removed,
            "meta_fixed": meta_fixed,
            "mermaid_degraded": degraded,
            "rewritten": rewritten,
        },
    }


async def _repair_meta_leaks(
    ctx: Ctx, text: str, hits: Sequence[Mapping[str, Any]]
) -> tuple[str, list[dict[str, Any]]]:
    """元信息泄漏：先试局部 REPAIR 改写该行，失败则直接剥离该行（R9）。"""
    lines = text.splitlines()
    fixed: list[dict[str, Any]] = []
    # 同一行可能命中多个禁词：按行去重，避免第二次处理时删到已移位的无辜行
    unique: dict[int, Mapping[str, Any]] = {}
    for hit in hits:
        unique.setdefault(int(hit["line"]), hit)
    for index, hit in enumerate(sorted(unique.values(), key=lambda h: int(h["line"]), reverse=True)):
        lineno = int(hit["line"]) - 1
        if not 0 <= lineno < len(lines):
            continue
        original = lines[lineno]
        replacement = ""
        try:
            system = assembler.assemble(
                [*BASE, BUILDER_PART_BY_TYPE.get(_patent_type(ctx), BUILDER_PART_BY_TYPE["invention"])],
                runtime_ctx={"case_card": _card(ctx)},
            )
            replacement = (
                await build_service.call_text(
                    ctx,
                    tag=f"meta.repair{index + 1}",
                    system=system,
                    user=(
                        "以下交底书正文中的一行出现了内部流程/元信息用语"
                        f"「{hit['word']}」，请改写为面向代理人的正式技术表述"
                        "（保留技术事实，删去元信息；只输出改写后的这一行，不要解释）：\n\n"
                        + original
                    ),
                )
            ).strip()
        except Exception as exc:  # noqa: BLE001 —— 改写不可用时退回剥离
            logger.info("元信息局部修复失败，改为剥离该行：%s", exc)
        if replacement and not any(w in replacement for w in build_service.META_FORBIDDEN):
            lines[lineno] = replacement
            fixed.append({"line": hit["line"], "word": hit["word"], "action": "repaired"})
        else:
            lines.pop(lineno)
            fixed.append({"line": hit["line"], "word": hit["word"], "action": "stripped"})
    return "\n".join(lines), fixed


# ---------------------------------------------------------------------------
# 步骤 8：deliver —— mermaid PNG / md / docx / pdf + 权利要求偏向点 [PAUSE]
# ---------------------------------------------------------------------------


def _export_docx_sync(markdown: str, filename_stem: str) -> tuple[bytes, dict[str, Any]]:
    """md → docx（内部先把 mermaid 围栏预渲染成 PNG）；返回 (docx 字节, 统计)。"""
    cfg = get_config()
    workdir = cfg.tmp_dir / f"disclosure_{ULID()}"
    workdir.mkdir(parents=True, exist_ok=True)
    md_path = workdir / f"{filename_stem}.md"
    docx_path = workdir / f"{filename_stem}.docx"
    try:
        md_path.write_text(markdown, encoding="utf-8")
        stats = export_docx_service.export_md_to_docx_sync(md_path, docx_path)
        return docx_path.read_bytes(), stats
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def _build_files(ctx: Ctx, markdown: str, case_title: str) -> dict[str, Any]:
    """落 disclosure_md / disclosure_docx（+ 可选 disclosure_pdf）三件交付物。"""
    label = _type_label(_patent_type(ctx))
    md_artifact = await artifacts_service.save_artifact(
        ctx.case_id,
        "disclosure_md",
        markdown,
        "md",
        title=case_title or None,
        md_text=markdown,
        run_group=ctx.run_group,
        iteration_type="initial",
        summary=f"交底书定稿（{label}）",
    )
    await _emit_artifact(ctx.case_id, md_artifact)

    downloads = [
        {"kind": md_artifact.kind, "artifact_id": md_artifact.id, "filename": md_artifact.filename}
    ]
    docx_artifact = None
    docx_error: str | None = None
    docx_stats: dict[str, Any] = {}
    stem = artifacts_service.strip_timestamp(md_artifact.filename)
    try:
        docx_bytes, docx_stats = await anyio.to_thread.run_sync(
            lambda: _export_docx_sync(markdown, stem)
        )
    except export_docx_service.DocxExportError as exc:
        docx_error = str(exc)
        logger.warning("交底书 docx 导出失败：%s", exc)
    else:
        docx_artifact = await artifacts_service.save_artifact(
            ctx.case_id,
            "disclosure_docx",
            docx_bytes,
            "docx",
            title=case_title or None,
            run_group=ctx.run_group,
            iteration_type="initial",
            source_artifact_id=md_artifact.id,
        )
        await _emit_artifact(ctx.case_id, docx_artifact)
        downloads.append(
            {
                "kind": docx_artifact.kind,
                "artifact_id": docx_artifact.id,
                "filename": docx_artifact.filename,
            }
        )

    pdf_artifact = None
    pdf_engine: str | None = None
    pdf_error: str | None = None
    if docx_artifact is not None:
        docx_path = Path(docx_artifact.stored_path)
        pdf_path = get_config().tmp_dir / f"{stem}_{ULID()}.pdf"
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            pdf_engine = await export_pdf_service.docx_to_pdf(docx_path, pdf_path)
        except export_pdf_service.PdfExportError as exc:
            pdf_error = str(exc)
            logger.info("交底书 PDF 导出不可用：%s", exc)
        if pdf_error is None and pdf_path.is_file():
            pdf_bytes = await anyio.to_thread.run_sync(pdf_path.read_bytes)
            pdf_artifact = await artifacts_service.save_artifact(
                ctx.case_id,
                "disclosure_pdf",
                pdf_bytes,
                "pdf",
                title=case_title or None,
                run_group=ctx.run_group,
                iteration_type="export",
                source_artifact_id=docx_artifact.id,
            )
            pdf_path.unlink(missing_ok=True)
            await _emit_artifact(ctx.case_id, pdf_artifact)
            downloads.append(
                {
                    "kind": pdf_artifact.kind,
                    "artifact_id": pdf_artifact.id,
                    "filename": pdf_artifact.filename,
                }
            )

    await _emit_doc_version(ctx.case_id, md_artifact, downloads=downloads)
    return {
        "md": json.loads(md_artifact.model_dump_json()),
        "docx": json.loads(docx_artifact.model_dump_json()) if docx_artifact else None,
        "docx_error": docx_error,
        "docx_stats": docx_stats,
        "pdf": json.loads(pdf_artifact.model_dump_json()) if pdf_artifact else None,
        "pdf_engine": pdf_engine,
        "pdf_error": pdf_error,
        "downloads": downloads,
    }


async def _save_mermaid_pngs(ctx: Ctx, markdown: str, case_title: str) -> list[dict[str, Any]]:
    """交付前把每个 mermaid 围栏渲染成 PNG 交付物。"""
    saved: list[dict[str, Any]] = []
    for index, (_s, _e, code) in enumerate(build_service.mermaid_blocks(markdown), 1):
        png, error = await build_service.render_mermaid(code)
        if png is None:
            await ctx.emit("log", {"message": f"第 {index} 幅图渲染失败，已在正文降级为文字清单：{error}"})
            continue
        artifact = await artifacts_service.save_artifact(
            ctx.case_id,
            "mermaid_png",
            png,
            "png",
            title=f"{case_title or '交底书'}_图{index}",
            run_group=ctx.run_group,
            iteration_type="initial",
            summary=f"图{index}",
        )
        await _emit_artifact(ctx.case_id, artifact)
        saved.append({"index": index, "artifact_id": artifact.id, "filename": artifact.filename})
    return saved


async def _claim_bias(ctx: Ctx, markdown: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """A8：STRUCT 产偏向点；basis_quote 必须是终稿子串（不命中 REPAIR → 仍不命中丢弃该组）。"""
    system = assembler.assemble(
        CLAIM_BIAS_PARTS,
        runtime_ctx={
            "case_card": _card(ctx),
            "extra": {"skeleton": ctx.state.get("skeleton")},
        },
    )
    user = (
        "以下为交底书终稿全文。请按契约输出「权利要求偏向点」JSON（1–2 组）；"
        "`basis_quote` 必须是终稿的逐字子串（10–60 字为宜），宁少勿编。\n\n"
        "===== 交底书终稿 =====\n" + markdown
    )
    bias: ClaimBias = await build_service.call_struct(
        ctx, ClaimBias, tag="claim_bias", system=system, user=user
    )

    def _bad(groups: Sequence[Any]) -> list[str]:
        problems: list[str] = []
        for gi, group in enumerate(groups, 1):
            for name in ("option_a", "option_b"):
                quote = str(getattr(getattr(group, name), "basis_quote", "") or "").strip()
                if not quote or faithfulness.locate(markdown, quote) is None:
                    problems.append(
                        f"第 {gi} 组 {name} 的 basis_quote 不是终稿的逐字子串：「{quote[:30]}」"
                    )
        return problems

    problems = _bad(bias.groups)
    repairs = 0
    while problems and repairs < MAX_BIAS_REPAIR:
        repairs += 1
        bias = await build_service.call_struct(
            ctx,
            ClaimBias,
            tag=f"claim_bias.repair{repairs}",
            system=system,
            user=(
                "上一版偏向点的 basis_quote 未通过终稿子串校验，问题如下：\n"
                + "\n".join(f"- {p}" for p in problems)
                + "\n\n请只摘录终稿中**真实存在**的原文片段作为 basis_quote，重新输出 JSON。\n\n"
                + user
            ),
            call_class="REPAIR",
        )
        problems = _bad(bias.groups)

    kept: list[dict[str, Any]] = []
    dropped = 0
    for group in bias.groups:
        quotes = [group.option_a.basis_quote, group.option_b.basis_quote]
        if all(q and faithfulness.locate(markdown, q) is not None for q in quotes):
            kept.append(group.model_dump())
        else:
            dropped += 1
    return kept, {"repairs": repairs, "dropped": dropped, "unresolved": problems}


async def deliver(ctx: Ctx) -> dict[str, Any]:
    """A8 交付：mermaid→PNG → md/docx/pdf → 偏向点门控（可跳过，仅存记录不入正文）。"""
    markdown = str(ctx.state.get("final_markdown") or ctx.state.get("draft_markdown") or "")
    if not markdown.strip():
        raise ValueError("缺少交底书终稿，无法交付")
    case_title = str(ctx.state.get("case_title") or (ctx.case or {}).get("title") or "")

    figures = await _save_mermaid_pngs(ctx, markdown, case_title)
    files = await _build_files(ctx, markdown, case_title)

    groups, bias_report = await _claim_bias(ctx, markdown)
    answer = _answer(
        await ctx.await_user(
            InteractionRequest(
                kind="claim_bias",
                schema={
                    "type": "object",
                    "properties": {
                        "skip": {"type": "boolean", "title": "跳过（不选偏向点）", "default": False},
                        "choices": {
                            "type": "array",
                            "title": "各组偏向选择",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "axis": {"type": "string", "title": "维度"},
                                    "choice": {
                                        "type": "string",
                                        "title": "倾向",
                                        "enum": ["option_a", "option_b", "none"],
                                    },
                                },
                            },
                        },
                        "apply": {
                            "type": "boolean",
                            "title": "按所选偏向对第五章做一次书式强化并另存新版本",
                            "default": False,
                        },
                        "note": {"type": "string", "title": "补充说明（可选）"},
                    },
                },
                prompt=(
                    "交底书已交付。以下「权利要求偏向点」仅供选用，**不会写入正文**；"
                    "如需以当前交付稿为基准对第五章做权利要求书式强化，可勾选后另存新版本。"
                    if groups
                    else "交底书已交付。本稿只有一条清晰保护主线，未产出可对举的偏向点，可直接跳过。"
                ),
                default={"skip": not groups, "choices": [], "apply": False, "note": "", "groups": groups},
            )
        )
    )

    selection = {
        "skipped": bool(answer.get("skip")) or not groups,
        "choices": [c for c in (answer.get("choices") or []) if isinstance(c, Mapping)],
        "note": str(answer.get("note") or ""),
        "applied": False,
    }

    if answer.get("apply") and selection["choices"] and groups:
        try:
            files, selection = await _apply_bias_revision(
                ctx, markdown, case_title, groups, selection, files
            )
        except Exception as exc:  # noqa: BLE001 —— 可选强化失败不影响已交付的定稿
            logger.warning("第五章偏向强化失败：%s", exc)
            selection["error"] = str(exc)

    lines = ["交底书已交付："]
    lines.append(f"- Markdown 定稿：{files['md']['filename']}")
    if files.get("docx"):
        lines.append(f"- Word 文件：{files['docx']['filename']}")
    elif files.get("docx_error"):
        lines.append(f"- Word 文件未能生成：{files['docx_error']}")
    if files.get("pdf"):
        lines.append(f"- PDF 文件：{files['pdf']['filename']}（引擎 {files.get('pdf_engine')}）")
    if figures:
        lines.append(f"- 图示 PNG：{len(figures)} 幅（已随 Word 嵌入）")
    unresolved = list((ctx.state.get("self_check") or {}).get("unresolved") or [])
    if unresolved:
        lines.append(f"- 需人工确认 {len(unresolved)} 项：")
        lines.extend(f"  · {u}" for u in unresolved[:5])
    await ctx.chat_delta("\n".join(lines))
    await ctx.chat_done()

    return {
        "deliver": {
            "figures": figures,
            "files": files,
            "claim_bias": {"groups": groups, **bias_report},
            "claim_bias_selection": selection,
        }
    }


async def _apply_bias_revision(
    ctx: Ctx,
    markdown: str,
    case_title: str,
    groups: Sequence[Mapping[str, Any]],
    selection: dict[str, Any],
    files: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """按用户所选偏向对第五章做一次书式强化，并另存新版本（正文其余部分不动）。"""
    ptype = _patent_type(ctx)
    chapters = dict(ctx.state.get("chapters") or {})
    section5 = build_service.section5_key(ptype)   # 外观无欲保护点章节 → None
    if section5 is None or section5 not in chapters:
        return files, selection
    wanted: list[str] = []
    for choice in selection["choices"]:
        axis = str(choice.get("axis") or "")
        pick = str(choice.get("choice") or "none")
        group = next((g for g in groups if str(g.get("axis")) == axis), None)
        if group is None or pick not in ("option_a", "option_b"):
            continue
        wanted.append(f"{axis}：{group[pick].get('label')}")
    if not wanted:
        return files, selection

    runtime = {
        "case_card": _card(ctx),
        "terminology_sheet": terminology.to_prompt_dict(ctx.state.get("terminology_sheet")),
        "outline": ctx.state.get("skeleton"),
        "extra": {"claim_bias": groups},
    }
    chapters[section5] = await build_service.rewrite_chapter(
        ctx,
        key=section5,
        text=chapters[section5],
        runtime=runtime,
        problems=[
            "按用户选定的权利要求书式偏向强化**第五章**（第四章保持衔接一致，不新增技术事实）：",
            *wanted,
        ],
        tag="bias.section5",
    )
    revised = build_service.assemble_document(
        chapters,
        case_title=case_title,
        contact=ctx.state.get("contact"),
        patent_type=ptype,
    )
    artifact = await artifacts_service.save_artifact(
        ctx.case_id,
        "disclosure_md",
        revised,
        "md",
        title=case_title or None,
        md_text=revised,
        run_group=ctx.run_group,
        iteration_type="rebuild",
        summary="按权利要求偏向点对第五章做书式强化",
    )
    await _emit_artifact(ctx.case_id, artifact)
    await _emit_doc_version(
        ctx.case_id,
        artifact,
        downloads=[
            {"kind": artifact.kind, "artifact_id": artifact.id, "filename": artifact.filename}
        ],
    )
    selection["applied"] = True
    selection["artifact_id"] = artifact.id
    files = {**files, "bias_md": json.loads(artifact.model_dump_json())}
    return files, selection


# ---------------------------------------------------------------------------
# 步骤表（按 patent_type 分派）
# ---------------------------------------------------------------------------


def build_invention_steps(case: dict[str, Any]) -> list[StepDef]:
    """发明分支的八步流水线（A1–A8）。"""
    return [
        StepDef(key="intake", name_zh="边界录入", handler=intake, gate="intake"),
        StepDef(key="material_scan", name_zh="材料消化", handler=material_scan),
        StepDef(key="points_mining", name_zh="专利点挖掘", handler=points_mining, gate="patent_points"),
        StepDef(key="prior_art_search", name_zh="联网查新", handler=prior_art_search, gate="prior_art"),
        StepDef(key="preview", name_zh="摘要预览", handler=preview, gate="preview_confirm"),
        StepDef(key="build", name_zh="分章成文", handler=build),
        StepDef(key="self_check", name_zh="组装与自检", handler=self_check),
        StepDef(key="deliver", name_zh="交付", handler=deliver, gate="claim_bias"),
    ]


def _steps_with_schema_fill() -> list[StepDef]:
    """实用新型 / 外观共用的九步（发明八步 + A3b「填表与线稿」）。

    两分支的 step_key 与门控完全一致，差异全在各步内部（prompt 资产、事实合同、
    附图入文规则、成文分支与 lint 束），故只需一份步骤表。
    """
    return [
        StepDef(key="intake", name_zh="边界录入", handler=intake, gate="intake"),
        StepDef(key="material_scan", name_zh="材料消化", handler=material_scan),
        StepDef(key="points_mining", name_zh="专利点挖掘", handler=points_mining, gate="patent_points"),
        StepDef(key="schema_fill", name_zh="填表与线稿", handler=schema_fill, gate="schema_fill"),
        StepDef(key="prior_art_search", name_zh="联网查新", handler=prior_art_search, gate="prior_art"),
        StepDef(key="preview", name_zh="摘要预览", handler=preview, gate="preview_confirm"),
        StepDef(key="build", name_zh="分章成文", handler=build),
        StepDef(key="self_check", name_zh="组装与自检", handler=self_check),
        StepDef(key="deliver", name_zh="交付", handler=deliver, gate="claim_bias"),
    ]


def build_utility_steps(case: dict[str, Any]) -> list[StepDef]:
    """实用新型分支的九步流水线（A1–A3b–A8）。"""
    return _steps_with_schema_fill()


def build_design_steps(case: dict[str, Any]) -> list[StepDef]:
    """外观设计分支的九步流水线（A1–A3b–A8）。"""
    return _steps_with_schema_fill()


STEP_BUILDERS = {
    "invention": build_invention_steps,
    "utility_model": build_utility_steps,
    "design": build_design_steps,
}


def build_steps(case: dict[str, Any]) -> list[StepDef]:
    """按案件 `patent_type` 选步骤工厂（缺省发明）。"""
    ptype = str(case.get("patent_type") or "invention")
    builder = STEP_BUILDERS.get(ptype)
    if builder is None:
        raise PatentTypeNotSupportedError(_unsupported_message(ptype))
    return builder(case)
