"""审查意见答复流水线（模块 D，prompt-porting-spec.md §2 模块D / D0–D5 + D-ingest）。

五步：

    notice_extract(门控 oa_issues) → retrieve(门控 oa_retrieve) → strategy(门控 oa_strategy)
        → draft → review_deliver(门控 confirm)

- **启动载荷**：`{file_id, related_case_id?}`（file_id = OA 通知书；缺省回落该案件
  最近一个已转出 markdown 的上传件）；
- **三处强制人审**（guardrails.md 暂停点 P1/P2/P3）：结构化结果核对、参考案例勾选、
  策略逐条勾选；D5 终稿确认沿用源规则的人审闸门逐字话术；
- **R16 防条款幻觉**：`statute` / `defect_type` 走服务端枚举校验，非法值连同错误清单
  REPAIR ≤2；仍不合格则把问题原样呈现给用户在 P1 手工修正；
- **检索方式明示**：retrieve 的门控 payload 必带 `retrieval_mode`（vector/brute/keyword），
  无库或未配 embedding 时正常走通（关键词或空结果 + 提示），不阻塞流水线；
- **修改后权项过 lint**：`amend_claims` 草稿抽出的修改后权项与本申请其余权项合并后
  跑 `services/claims_lint`，error 级违规带清单 REPAIR ≤2；
- **跨条一致性**：确定性代码查（同一权项多条修改冲突 / 编号连续 / 引用基础）+ 一次
  AUDIT 复核，结果进「待人工确认事项」呈现给用户。

交付物：`oa_response_md` + `oa_response_docx`；可选「归档回案例库」产 `draft` 状态的
案例笔记（闭环，见 services/oa_library）。
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
from ulid import ULID

from ..config import get_config
from ..db import database as db
from ..models.oa import (
    DEFECT_TYPES,
    DEFECT_TYPE_LABEL,
    DEFECT_TYPE_TO_BASE,
    NOTICE_KINDS,
    PATENT_TYPES,
    RESPONSE_STRATEGIES,
    RESPONSE_STRATEGY_LABEL,
    RESPONSE_TO_CASE_STRATEGY,
    ConsistencyAudit,
    IssueStrategy,
    NoticeStruct,
    lint_issue_strategy,
    lint_notice_struct,
)
from ..services import artifacts as artifacts_service
from ..services import paths as paths_service
from ..services import assembler, assets_loader, claims_lint, oa_library, vector
from ..services import disclosure_build as build_service
from ..services import export_docx as export_docx_service
from ..services.sse import hub
from .engine import Ctx, InteractionRequest, StepDef

logger = logging.getLogger(__name__)

#: 文档通道 doc_id（前端右侧文档面板按 doc_id 归集流式正文）
DOC_ID = "oa_response"

#: 各类 REPAIR 上限（§0.1：REPAIR ≤2 → 降级策略）
MAX_NOTICE_REPAIR = 2
MAX_STRATEGY_REPAIR = 2
MAX_CLAIMS_REPAIR = 2

#: 每条 issue 检索的案例数
RETRIEVE_K = 5
#: 注入 prompt 的案例节选长度
CASE_EXCERPT_CHARS = 600
#: 注入 prompt 的通知书/申请文件长度上限（上下文经济，§3.6）
NOTICE_CHARS = 20000
APPLICATION_CHARS = 12000

BASE = ["common/system_base"]
GUARD = "oa/guardrails"
NOTICE_PARTS = [*BASE, GUARD, "oa/notice_struct"]
STRATEGY_PARTS = [*BASE, GUARD, "common/desensitization", "oa/strategy"]
DRAFT_PARTS = [*BASE, GUARD, "common/desensitization", "common/terminology_rules", "oa/respond_issue"]
AUDIT_PARTS = [*BASE, GUARD]

DRAFT_BANNER = "草稿·须人工复核"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _payload(ctx: Ctx) -> dict[str, Any]:
    return ctx.start_payload if isinstance(ctx.start_payload, dict) else {}


def _answer(value: Any) -> dict[str, Any]:
    """门控回填：非 dict（含 None）一律视为空表单，走缺省分支。"""
    return dict(value) if isinstance(value, Mapping) else {}


def _case_card(ctx: Ctx) -> dict[str, Any]:
    """注入 prompt 的案件卡片。"""
    case = getattr(ctx, "case", None) or {}
    state = getattr(ctx, "state", None) or {}
    notice = state.get("notice") or {}
    return {
        "case_id": getattr(ctx, "case_id", ""),
        "module": "oa（审查意见答复）",
        "case_title": case.get("title") or "",
        "application_no": notice.get("application_no") or "",
        "notice_kind": notice.get("notice_kind") or "",
        "patent_type": notice.get("patent_type") or case.get("patent_type") or "",
        "deadline": notice.get("deadline") or "",
        "draft_notice": DRAFT_BANNER,
    }


async def _emit_artifact(case_id: str, artifact) -> None:
    await hub.emit(case_id, "artifact_created", json.loads(artifact.model_dump_json()))


async def _emit_doc_version(case_id: str, artifact, downloads: list[dict[str, Any]]) -> None:
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
            "downloads": downloads,
            "iteration_type": artifact.iteration_type,
            "summary": artifact.summary,
            "created_at": artifact.created_at,
        },
    )


_HUMAN_GATE_FALLBACK = (
    "以下为审查答复【草稿】，须代理人/发明人复核后再递交。\n"
    "请回复：确认仅作内部草稿 / 需要修改策略 / 暂停"
)


def human_gate_prompt() -> str:
    """终稿人审话术——逐字取自 guardrails.md 的「人审闸门」代码块（源规则原文）。"""
    try:
        body = assets_loader.get_text("prompts/oa/guardrails")
    except KeyError:  # pragma: no cover —— 资产缺失时用内置副本兜底
        return _HUMAN_GATE_FALLBACK
    for block in re.findall(r"```[a-zA-Z]*\r?\n(.*?)```", body, re.DOTALL):
        if "确认仅作内部草稿" in block:
            return block.strip()
    return _HUMAN_GATE_FALLBACK


# ---------------------------------------------------------------------------
# 权利要求解析与 lint
# ---------------------------------------------------------------------------

_CLAIM_START_RE = re.compile(r"^\s*(\d{1,3})\s*[\.．、:：]\s*(\S.*)$")
_AMENDED_BLOCK_RE = re.compile(
    r"【\s*修改后权利要求[^】]*】\s*\n(.*?)(?=\n\s*【|\n\s*#{1,6}\s|\Z)", re.DOTALL
)


def parse_claims(text: str) -> dict[int, str]:
    """从权利要求书文本解析 `{权项号: 正文}`（正文含编号前缀，供 claims_lint 使用）。"""
    claims: dict[int, str] = {}
    current: int | None = None
    buf: list[str] = []

    def flush() -> None:
        if current is not None and buf:
            body = " ".join(x.strip() for x in buf if x.strip())
            if body:
                claims[current] = f"{current}. {body}"

    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(("#", ">", "|", "---")):
            continue
        m = _CLAIM_START_RE.match(line)
        if m:
            flush()
            current, buf = int(m.group(1)), [m.group(2)]
        elif current is not None:
            buf.append(line)
    flush()
    return claims


def extract_amended_claims(markdown: str) -> dict[int, str]:
    """从答复草稿抽出「【修改后权利要求（全文）】」块里的权项。"""
    m = _AMENDED_BLOCK_RE.search(markdown or "")
    if m is None:
        return {}
    return parse_claims(m.group(1))


def merged_claim_list(base: Mapping[int, str], amended: Mapping[int, str]) -> list[str]:
    """基准权项 + 修改后权项 → 按权项号升序的完整清单（claims_lint 入参）。"""
    merged = {int(k): str(v) for k, v in base.items()}
    merged.update({int(k): str(v) for k, v in amended.items()})
    return [merged[no] for no in sorted(merged)]


def lint_amended_claims(
    base: Mapping[int, str], amended: Mapping[int, str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """对「基准 + 修改」的完整权项集合跑 claims_lint。

    返回 (与修改权项相关的 error 级发现, 全部发现)；发明名称一致性不在本模块校验
    （OA 阶段没有可信的发明名称来源），故 invention_name 传空串。
    """
    claims = merged_claim_list(base, amended)
    if not claims:
        return [], []
    findings = claims_lint.lint_claims(claims, "")
    touched = {int(k) for k in amended}
    errors = [
        f for f in findings
        if f.get("severity") == "error" and int(f.get("claim_no") or 0) in touched
    ]
    return errors, findings


# ---------------------------------------------------------------------------
# 材料读取
# ---------------------------------------------------------------------------


def _file_rows_sync(case_id: str) -> list[dict[str, Any]]:
    rows = db.query_all(
        "SELECT * FROM files WHERE case_id=? AND md_path IS NOT NULL ORDER BY created_at ASC",
        (case_id,),
    )
    return [dict(r) for r in rows]


def _read_text_sync(path: str | None) -> str:
    if not path:
        return ""
    p = paths_service.resolve_existing(path)
    if p is None:
        return ""
    return p.read_text(encoding="utf-8", errors="replace")


def _load_materials_sync(case_id: str, file_id: str) -> dict[str, Any]:
    """通知书正文 + 其余申请文件正文（file_id 缺省时取最近一个已转换的上传件）。"""
    rows = _file_rows_sync(case_id)
    if not rows:
        raise ValueError("本案件尚未上传可解析的审查意见通知书（需先上传 PDF/DOCX/MD）")
    notice_row: dict[str, Any] | None = None
    if file_id:
        notice_row = next((r for r in rows if str(r["id"]) == str(file_id)), None)
        if notice_row is None:
            raise ValueError(f"通知书文件不存在或不属于本案件：{file_id}")
    else:
        notice_row = rows[-1]
    others = [r for r in rows if str(r["id"]) != str(notice_row["id"])]
    return {
        "notice_file_id": str(notice_row["id"]),
        "notice_name": str(notice_row["orig_name"]),
        "notice_text": _read_text_sync(str(notice_row["md_path"]))[:NOTICE_CHARS],
        "application_docs": [
            {
                "file_id": str(r["id"]),
                "name": str(r["orig_name"]),
                "text": _read_text_sync(str(r["md_path"]))[:APPLICATION_CHARS],
            }
            for r in others
        ],
    }


_CLAIMS_HINT_RE = re.compile(r"权\s*利\s*要\s*求\s*书?")


def guess_claims_text(docs: Sequence[Mapping[str, Any]]) -> str:
    """从申请文件里挑出最像权利要求书的一段（供人审时确认/修正）。"""
    best = ""
    for doc in docs:
        text = str(doc.get("text") or "")
        name = str(doc.get("name") or "")
        if not text.strip():
            continue
        hit = _CLAIMS_HINT_RE.search(name) or _CLAIMS_HINT_RE.search(text[:2000])
        if not hit:
            continue
        start = text.find("权利要求书")
        segment = text[start:] if start >= 0 else text
        if len(parse_claims(segment)) > len(parse_claims(best)):
            best = segment
    if best:
        return best[:APPLICATION_CHARS]
    # 没有明显的权利要求书文件时，取解析出权项最多的那份
    for doc in docs:
        text = str(doc.get("text") or "")
        if len(parse_claims(text)) > len(parse_claims(best)):
            best = text
    return best[:APPLICATION_CHARS] if len(parse_claims(best)) >= 1 else ""


# ---------------------------------------------------------------------------
# 步骤 1：notice_extract —— 通知书结构化 + 枚举校验 [PAUSE P1]
# ---------------------------------------------------------------------------

ISSUE_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "title": "条目编号", "readOnly": True},
        "statute": {
            "type": "string",
            "title": "法条（照抄通知书原文；未写明填「未明示」）",
        },
        "defect_type": {
            "type": "string",
            "title": "缺陷类型",
            "enum": list(DEFECT_TYPES),
            "enumNames": [DEFECT_TYPE_LABEL[d] for d in DEFECT_TYPES],
        },
        "claims_involved": {
            "type": "array",
            "title": "涉及权利要求",
            "items": {"type": "integer"},
        },
        "examiner_argument_digest": {"type": "string", "title": "审查员观点摘要（≤200字）"},
        "cited_documents": {
            "type": "array",
            "title": "对比文件",
            "items": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "title": "编号（D1）"},
                    "ref_label": {"type": "string", "title": "通知书原文标号"},
                    "identifier": {"type": "string", "title": "公开号"},
                    "used_against": {
                        "type": "array", "title": "用于评述的权项", "items": {"type": "integer"}
                    },
                },
            },
        },
        "note": {"type": "string", "title": "备注"},
        "drop": {"type": "boolean", "title": "删除本条", "default": False},
    },
}

ISSUES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "notice_kind": {
            "type": "string",
            "title": "通知书种类",
            "enum": list(NOTICE_KINDS),
            "enumNames": ["审查意见通知书", "补正通知书", "驳回决定", "其他"],
        },
        "notice_no": {"type": "integer", "title": "第几次通知书"},
        "application_no": {"type": "string", "title": "申请号"},
        "patent_type": {
            "type": "string",
            "title": "专利类型",
            "enum": list(PATENT_TYPES),
            "enumNames": ["发明", "实用新型", "外观设计"],
        },
        "deadline": {"type": "string", "title": "答复期限（YYYY-MM-DD，可留空）"},
        "issues": {"type": "array", "title": "逐条缺陷（可增删改）", "items": ISSUE_ITEM_SCHEMA},
        "claims_text": {"type": "string", "title": "本申请权利要求书原文（供修改对照与权项 lint）"},
    },
}


async def _struct_notice(ctx: Ctx, materials: Mapping[str, Any]) -> tuple[NoticeStruct, dict[str, Any]]:
    """D1 STRUCT + 枚举校验（非法值带错误清单 REPAIR ≤2）。"""
    system = assembler.assemble(
        NOTICE_PARTS,
        runtime_ctx={
            "case_card": _case_card(ctx),
            "extra": {
                "notice_file": materials.get("notice_name"),
                "application_files": [d["name"] for d in materials.get("application_docs") or []],
            },
        },
    )
    docs = materials.get("application_docs") or []
    user = (
        "以下为审查意见通知书的服务端抽取文本"
        + ("（另附本申请文件摘录供核对权项号）" if docs else "")
        + "，请按输出契约结构化为一个 JSON 对象。\n\n"
        + "===== 通知书正文 =====\n"
        + str(materials.get("notice_text") or "")
    )
    if docs:
        user += "\n\n===== 本申请文件摘录 =====\n" + "\n\n".join(
            f"--- {d['name']} ---\n{str(d['text'])[:4000]}" for d in docs[:3]
        )

    notice: NoticeStruct = await build_service.call_struct(
        ctx, NoticeStruct, tag="notice", system=system, user=user
    )
    problems = lint_notice_struct(notice)
    repairs = 0
    while problems and repairs < MAX_NOTICE_REPAIR:
        repairs += 1
        await ctx.emit(
            "log",
            {
                "message": (
                    f"通知书结构化未通过枚举校验（第 {repairs} 次修复）："
                    + "；".join(problems[:3])
                ),
                "kind": "enum_gate",
            },
        )
        notice = await build_service.call_struct(
            ctx,
            NoticeStruct,
            tag=f"notice.repair{repairs}",
            system=system,
            user=(
                "上一版结构化结果未通过服务端枚举校验，问题如下：\n"
                + "\n".join(f"- {p}" for p in problems)
                + "\n\n请修正后重新输出完整 JSON；条款一律照抄通知书原文，"
                "通知书未写明法条时填「未明示」，不得臆造。\n\n"
                + user
            ),
            call_class="REPAIR",
        )
        problems = lint_notice_struct(notice)
    return notice, {"repairs": repairs, "unresolved": problems}


def _apply_issue_edits(notice: NoticeStruct, answer: Mapping[str, Any]) -> NoticeStruct:
    """把用户在 P1 的逐条编辑合并回结构化结果（未提交 issues 时保持原样）。"""
    data = notice.model_dump()
    for key in ("notice_kind", "application_no", "patent_type", "deadline", "notice_no"):
        if key in answer and answer[key] not in (None, ""):
            data[key] = answer[key]
    edits = answer.get("issues")
    if isinstance(edits, list) and edits:
        by_id = {str(i.get("id") or ""): dict(i) for i in data["issues"]}
        merged: list[dict[str, Any]] = []
        for index, item in enumerate(edits):
            if not isinstance(item, Mapping):
                continue
            item = dict(item)
            if item.pop("drop", False):
                continue
            base = dict(by_id.get(str(item.get("id") or ""), {}))
            base.update({k: v for k, v in item.items() if v not in (None, "")})
            base.setdefault("id", f"I{index + 1}")
            merged.append(base)
        data["issues"] = merged
    return NoticeStruct.model_validate(data)


async def notice_extract(ctx: Ctx) -> dict[str, Any]:
    """D1 通知书结构化 → 枚举校验（REPAIR ≤2）→ [PAUSE P1] 用户逐条核对。"""
    payload = _payload(ctx)
    materials = await db.arun(
        _load_materials_sync, ctx.case_id, str(payload.get("file_id") or "")
    )
    notice, report = await _struct_notice(ctx, materials)
    claims_text = guess_claims_text(materials.get("application_docs") or [])

    prompt = (
        "以下为通知书结构化结果，请逐条核对后再进入检索与策略阶段"
        "（这是防条款幻觉的强制人审闸门：法条须与通知书原文逐字一致）。"
    )
    if report["unresolved"]:
        prompt += "\n\n服务端校验仍有以下问题，请一并修正：\n" + "\n".join(
            f"- {p}" for p in report["unresolved"]
        )
    if not claims_text.strip():
        prompt += "\n\n未在已上传文件中识别到本申请权利要求书，请粘贴权项原文（用于修改对照与权项校验）。"

    answer = _answer(
        await ctx.await_user(
            InteractionRequest(
                kind="oa_issues",
                schema=ISSUES_SCHEMA,
                prompt=prompt,
                default={
                    **notice.model_dump(),
                    "claims_text": claims_text,
                    "defect_type_options": [
                        {"value": d, "label": DEFECT_TYPE_LABEL[d]} for d in DEFECT_TYPES
                    ],
                    "lint_problems": report["unresolved"],
                    "repairs": report["repairs"],
                },
            )
        )
    )
    notice = _apply_issue_edits(notice, answer)
    claims_text = str(answer.get("claims_text") or claims_text)
    final_problems = lint_notice_struct(notice)
    if final_problems:
        await ctx.emit(
            "log",
            {
                "message": "人审后仍有校验提示（已按用户确认继续）：" + "；".join(final_problems[:3]),
                "kind": "enum_gate",
            },
        )

    title = notice.notice_label()
    if notice.application_no:
        title += f" 答复（{notice.application_no}）"
    await build_service.set_case_title(ctx, title)

    base_claims = parse_claims(claims_text)
    await ctx.emit(
        "log",
        {
            "message": (
                f"通知书结构化完成：{notice.notice_label()}，共 {len(notice.issues)} 条缺陷；"
                f"本申请权项 {len(base_claims)} 项。"
            )
        },
    )
    return {
        "notice": notice.model_dump(),
        "notice_lint": {**report, "after_review": final_problems},
        "materials": {
            "notice_file_id": materials.get("notice_file_id"),
            "notice_name": materials.get("notice_name"),
            "notice_text": materials.get("notice_text"),
            "application_docs": materials.get("application_docs"),
        },
        "claims_text": claims_text,
        "base_claims": {str(k): v for k, v in base_claims.items()},
    }


# ---------------------------------------------------------------------------
# 步骤 2：retrieve —— 逐 issue 案例检索 [PAUSE P-retrieve]
# ---------------------------------------------------------------------------


def _issue_query(issue: Mapping[str, Any]) -> str:
    """逐 issue 构造检索查询（defect_type + statute + 观点摘要）。"""
    defect = str(issue.get("defect_type") or "")
    label = DEFECT_TYPE_LABEL.get(defect, defect)
    return " ".join(
        x for x in (
            label,
            str(issue.get("statute") or ""),
            str(issue.get("examiner_argument_digest") or ""),
        ) if x
    ).strip()


def _issue_filters(issue: Mapping[str, Any], patent_type: str | None) -> dict[str, Any]:
    """元数据预过滤条件（statutes / defect_types / patent_type）。"""
    filters: dict[str, Any] = {}
    defect = str(issue.get("defect_type") or "").strip()
    if defect:
        values = [defect]
        base = DEFECT_TYPE_TO_BASE.get(defect)
        if base and base not in values:
            values.append(base)
        filters["defect_types"] = values
    statute = str(issue.get("statute") or "").strip()
    if statute and statute != "未明示":
        filters["statutes"] = [statute]
    if patent_type in PATENT_TYPES:
        filters["patent_type"] = patent_type
    return filters


def case_viewer(ctx: Ctx | None) -> dict[str, Any] | None:
    """流水线内的可见性主体 = 案件属主（M8 数据隔离：不得检索到他人的私有案例）。"""
    if ctx is None:
        return None
    owner = str((ctx.case or {}).get("user_id") or "")
    return {"id": owner, "role": "user"} if owner else None


async def _search_issue(
    issue: Mapping[str, Any],
    patent_type: str | None,
    viewer: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """单条 issue 的检索：先严格过滤，无命中则逐级放宽（记录 relaxed）。"""
    query = _issue_query(issue)
    strict = _issue_filters(issue, patent_type)
    result = await vector.search(query, k=RETRIEVE_K, filters=strict, viewer=viewer)
    relaxed: list[str] = []
    if not result["hits"] and strict.get("statutes"):
        loose = {k: v for k, v in strict.items() if k != "statutes"}
        result = await vector.search(query, k=RETRIEVE_K, filters=loose, viewer=viewer)
        relaxed.append("statutes")
    if not result["hits"] and (strict.get("defect_types") or strict.get("patent_type")):
        result = await vector.search(query, k=RETRIEVE_K, filters={}, viewer=viewer)
        relaxed.extend(k for k in ("defect_types", "patent_type") if strict.get(k))
    result["relaxed"] = relaxed
    result["query"] = query
    result["filters_applied"] = strict
    return result


async def retrieve(ctx: Ctx) -> dict[str, Any]:
    """D2 案例检索：元数据预过滤 + 向量/暴搜/关键词 → [PAUSE] 用户勾选参考案例。"""
    notice = dict(ctx.state.get("notice") or {})
    issues = list(notice.get("issues") or [])
    patent_type = notice.get("patent_type")

    per_issue: list[dict[str, Any]] = []
    modes: list[str] = []
    viewer = case_viewer(ctx)
    for position, issue in enumerate(issues, start=1):
        await ctx.progress(
            "为每条审查意见检索相似案例",
            index=position, total=len(issues),
            waiting_for="案例库检索",
        )
        result = await _search_issue(issue, patent_type, viewer)
        modes.append(str(result.get("mode") or "keyword"))
        per_issue.append(
            {
                "issue_id": str(issue.get("id") or ""),
                "query": result.get("query") or "",
                "retrieval_mode": result.get("mode"),
                "relaxed": result.get("relaxed") or [],
                "total_candidates": result.get("total_candidates") or 0,
                "message": result.get("message") or "",
                "error": result.get("error") or "",
                "hits": result.get("hits") or [],
                "selected": [h["library_id"] for h in (result.get("hits") or [])[:2]],
            }
        )
        await ctx.emit(
            "search_progress",
            {
                "step_key": ctx.step_key,
                "issue_id": str(issue.get("id") or ""),
                "retrieval_mode": result.get("mode"),
                "hits": len(result.get("hits") or []),
                "message": result.get("message") or "",
            },
        )

    overall = modes[0] if modes else ("vector" if vector.is_configured() else "keyword")
    if len(set(modes)) > 1:
        overall = "keyword" if "keyword" in modes else "brute"
    total_hits = sum(len(x["hits"]) for x in per_issue)
    notes = [x["message"] for x in per_issue if x["message"]]

    prompt = (
        f"案例检索完成（检索方式：{ {'vector': '向量检索', 'brute': '余弦暴搜', 'keyword': '关键词匹配'}.get(overall, overall) }）"
        f"，共命中 {total_hits} 条案例节选。请逐条勾选要作为参考的历史案例；"
        "未勾选或无命中时，草稿将只依据本案文本与通用审查规则说理，不会假装引用历史案例。"
    )
    if notes:
        prompt += "\n\n" + "\n".join(f"- {n}" for n in dict.fromkeys(notes))

    answer = _answer(
        await ctx.await_user(
            InteractionRequest(
                kind="oa_retrieve",
                schema={
                    "type": "object",
                    "properties": {
                        "selections": {
                            "type": "array",
                            "title": "逐条 issue 的参考案例勾选",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "issue_id": {"type": "string", "title": "条目"},
                                    "library_ids": {
                                        "type": "array",
                                        "title": "选中的案例",
                                        "items": {"type": "string"},
                                    },
                                },
                            },
                        }
                    },
                },
                prompt=prompt,
                default={
                    "retrieval_mode": overall,
                    "issues": per_issue,
                    "selections": [
                        {"issue_id": x["issue_id"], "library_ids": x["selected"]}
                        for x in per_issue
                    ],
                },
            )
        )
    )

    picks = {
        str(s.get("issue_id") or ""): [str(i) for i in (s.get("library_ids") or [])]
        for s in (answer.get("selections") or [])
        if isinstance(s, Mapping)
    }
    for item in per_issue:
        if item["issue_id"] in picks:
            wanted = set(picks[item["issue_id"]])
            item["selected"] = [h["library_id"] for h in item["hits"] if h["library_id"] in wanted]

    selected_total = sum(len(x["selected"]) for x in per_issue)
    await ctx.emit(
        "log",
        {
            "message": (
                f"参考案例已确认：命中 {total_hits} 条，选用 {selected_total} 条"
                f"（检索方式 {overall}）。"
            )
        },
    )
    return {
        "retrieval": {
            "retrieval_mode": overall,
            "modes": modes,
            "issues": per_issue,
            "hit_count": total_hits,
            "selected_count": selected_total,
        }
    }


def _selected_cases(ctx: Ctx, issue_id: str) -> list[dict[str, Any]]:
    """某 issue 用户勾选的案例节选（注入 strategy / draft 调用）。"""
    retrieval = ctx.state.get("retrieval") or {}
    for item in retrieval.get("issues") or []:
        if str(item.get("issue_id")) != str(issue_id):
            continue
        wanted = set(item.get("selected") or [])
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for hit in item.get("hits") or []:
            lib = str(hit.get("library_id"))
            if lib not in wanted or lib in seen:
                continue
            seen.add(lib)
            fm = hit.get("frontmatter") or {}
            out.append(
                {
                    "case_id": str(fm.get("case_id") or lib),
                    "title": str(fm.get("title") or ""),
                    "statutes": fm.get("statutes") or [],
                    "defect_types": fm.get("defect_types") or [],
                    "outcome": str(fm.get("outcome") or ""),
                    "strategy": fm.get("strategy") or [],
                    "excerpt": str(hit.get("chunk_text") or "")[:CASE_EXCERPT_CHARS],
                    "score": hit.get("score"),
                }
            )
        return out
    return []


# ---------------------------------------------------------------------------
# 步骤 3：strategy —— 逐 issue 策略选项 [PAUSE P2]
# ---------------------------------------------------------------------------


async def _issue_strategy(ctx: Ctx, issue: Mapping[str, Any]) -> tuple[IssueStrategy, dict[str, Any]]:
    """单条 issue 的策略 STRUCT + 枚举/自洽校验（REPAIR ≤2）。"""
    issue_id = str(issue.get("id") or "")
    cases = _selected_cases(ctx, issue_id)
    retrieval = ctx.state.get("retrieval") or {}
    system = assembler.assemble(
        STRATEGY_PARTS,
        runtime_ctx={
            "case_card": _case_card(ctx),
            "extra": {
                "issue": issue,
                "retrieval_mode": retrieval.get("retrieval_mode"),
                "retrieved_cases": cases,
                "claims_text": str(ctx.state.get("claims_text") or "")[:APPLICATION_CHARS],
            },
        },
    )
    user = (
        f"请为条目 {issue_id}（{issue.get('statute')} / "
        f"{DEFECT_TYPE_LABEL.get(str(issue.get('defect_type')), issue.get('defect_type'))}，"
        f"涉及权项 {issue.get('claims_involved') or '（说明书/摘要）'}）给出可勾选的答复策略选项，"
        f'只输出一个 JSON 对象：{{"issue_id":"{issue_id}","options":[…],"recommended":"…"}}。'
        + ("\n检索命中为空，禁止假装引用历史案例。" if not cases else "")
    )
    plan: IssueStrategy = await build_service.call_struct(
        ctx, IssueStrategy, tag=f"strategy.{issue_id}", system=system, user=user
    )
    if not plan.issue_id:
        plan.issue_id = issue_id
    problems = lint_issue_strategy(plan)
    repairs = 0
    while problems and repairs < MAX_STRATEGY_REPAIR:
        repairs += 1
        await ctx.emit(
            "log",
            {"message": f"{issue_id} 策略选项未通过校验（第 {repairs} 次修复）：{'；'.join(problems[:3])}"},
        )
        plan = await build_service.call_struct(
            ctx,
            IssueStrategy,
            tag=f"strategy.{issue_id}.repair{repairs}",
            system=system,
            user=(
                "上一版策略选项未通过服务端校验，问题如下：\n"
                + "\n".join(f"- {p}" for p in problems)
                + "\n\n请修正后重新输出完整 JSON。\n\n"
                + user
            ),
            call_class="REPAIR",
        )
        if not plan.issue_id:
            plan.issue_id = issue_id
        problems = lint_issue_strategy(plan)
    return plan, {"repairs": repairs, "unresolved": problems}


async def strategy(ctx: Ctx) -> dict[str, Any]:
    """D3 策略选项：逐 issue STRUCT → [PAUSE P2] 用户逐条勾选（支持备注）。"""
    notice = dict(ctx.state.get("notice") or {})
    issues = list(notice.get("issues") or [])
    plans: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    for position, issue in enumerate(issues, start=1):
        await ctx.progress(
            "为每条审查意见拟策略",
            index=position, total=len(issues),
            detail=f"第 {position} 条：{str(issue.get('statute') or issue.get('id') or '')}"[:60],
            waiting_for="模型",
        )
        plan, report = await _issue_strategy(ctx, issue)
        plans.append(plan.model_dump())
        reports.append({"issue_id": plan.issue_id, **report})

    defaults = []
    for issue, plan in zip(issues, plans):
        options = plan.get("options") or []
        recommended = plan.get("recommended") or (options[0]["strategy"] if options else "argue_only")
        defaults.append(
            {
                "issue_id": str(issue.get("id") or ""),
                "strategy": recommended,
                "note": "",
            }
        )

    answer = _answer(
        await ctx.await_user(
            InteractionRequest(
                kind="oa_strategy",
                schema={
                    "type": "object",
                    "properties": {
                        "choices": {
                            "type": "array",
                            "title": "逐条策略勾选",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "issue_id": {"type": "string", "title": "条目"},
                                    "strategy": {
                                        "type": "string",
                                        "title": "采用策略",
                                        "enum": list(RESPONSE_STRATEGIES),
                                        "enumNames": [
                                            RESPONSE_STRATEGY_LABEL[s] for s in RESPONSE_STRATEGIES
                                        ],
                                    },
                                    "note": {"type": "string", "title": "备注（会带入起草）"},
                                },
                                "required": ["issue_id", "strategy"],
                            },
                        }
                    },
                },
                prompt=(
                    "以下为逐条答复策略选项（修改类选项已给出超范围风险评估）。"
                    "请逐条勾选要采用的策略，可改选非推荐项并填写备注。"
                ),
                default={"choices": defaults, "strategies": plans},
            )
        )
    )

    picks = {
        str(c.get("issue_id") or ""): c
        for c in (answer.get("choices") or [])
        if isinstance(c, Mapping)
    }
    choices: list[dict[str, Any]] = []
    for default, plan in zip(defaults, plans):
        issue_id = default["issue_id"]
        picked = picks.get(issue_id) or {}
        value = str(picked.get("strategy") or default["strategy"])
        if value not in RESPONSE_STRATEGIES:
            value = default["strategy"]
        option = next(
            (o for o in (plan.get("options") or []) if o.get("strategy") == value), None
        )
        choices.append(
            {
                "issue_id": issue_id,
                "strategy": value,
                "note": str(picked.get("note") or ""),
                "amendment_sketch": str((option or {}).get("amendment_sketch") or ""),
                "rationale": str((option or {}).get("rationale") or ""),
                "risk": str((option or {}).get("risk") or ""),
                "from_recommended": value == (plan.get("recommended") or ""),
            }
        )

    await ctx.emit(
        "log",
        {
            "message": "答复策略已确认：" + "；".join(
                f"{c['issue_id']}→{RESPONSE_STRATEGY_LABEL.get(c['strategy'], c['strategy'])}"
                for c in choices
            )
        },
    )
    return {
        "strategies": plans,
        "strategy_reports": reports,
        "strategy_choices": choices,
    }


# ---------------------------------------------------------------------------
# 步骤 4：draft —— 逐条起草（GEN 流式）+ 修改后权项 lint
# ---------------------------------------------------------------------------


def _lint_message(findings: Sequence[Mapping[str, Any]]) -> str:
    return "\n".join(
        f"- 权利要求{f.get('claim_no')}（{f.get('rule')}）：{f.get('message')}" for f in findings
    )


async def _draft_issue(
    ctx: Ctx,
    issue: Mapping[str, Any],
    choice: Mapping[str, Any],
    base_claims: Mapping[int, str],
) -> dict[str, Any]:
    """单条 issue 的起草（流式 doc 通道）+ amend_claims 的权项 lint（REPAIR ≤2）。"""
    issue_id = str(issue.get("id") or "")
    strategy_value = str(choice.get("strategy") or "argue_only")
    cases = _selected_cases(ctx, issue_id)
    retrieval = ctx.state.get("retrieval") or {}
    index = [str(i.get("id")) for i in (ctx.state.get("notice") or {}).get("issues") or []]
    order = index.index(issue_id) + 1 if issue_id in index else 1

    system = assembler.assemble(
        DRAFT_PARTS,
        runtime_ctx={
            "case_card": _case_card(ctx),
            "extra": {
                "issue": issue,
                "issue_order": order,
                "strategy": strategy_value,
                "strategy_label": RESPONSE_STRATEGY_LABEL.get(strategy_value, strategy_value),
                "amendment_sketch": choice.get("amendment_sketch") or "",
                "user_note": choice.get("note") or "",
                "risk": choice.get("risk") or "",
                "retrieval_mode": retrieval.get("retrieval_mode"),
                "retrieved_cases": cases,
                "claims_text": str(ctx.state.get("claims_text") or "")[:APPLICATION_CHARS],
            },
        },
    )
    user = (
        f"请起草条目 {issue_id}（通知书第 {order} 条）的答复。\n"
        f"- 法条：{issue.get('statute')}\n"
        f"- 缺陷类型：{DEFECT_TYPE_LABEL.get(str(issue.get('defect_type')), issue.get('defect_type'))}\n"
        f"- 涉及权项：{issue.get('claims_involved') or '（说明书/摘要）'}\n"
        f"- 用户选定策略：{strategy_value}"
        f"（{RESPONSE_STRATEGY_LABEL.get(strategy_value, strategy_value)}）\n"
        + (f"- 修改草图：{choice.get('amendment_sketch')}\n" if choice.get("amendment_sketch") else "")
        + (f"- 用户备注：{choice.get('note')}\n" if choice.get("note") else "")
        + (
            "- 本条无可参考的检索命中，请只依据本案文本与通用审查规则说理，"
            "禁止编造或暗示存在历史案例支持。\n"
            if not cases
            else "- 引用命中案例时须写明 case_id、为何可参考、与本案差异。\n"
        )
        + (
            "- 本条采用 amend_claims：必须输出【修改对照】表与【修改后权利要求（全文）】，"
            "修改后权项将由服务端跑权项 lint。\n"
            if strategy_value == "amend_claims"
            else ""
        )
        + "\n只输出该条答复的 markdown。"
    )

    markdown = await build_service.stream_gen(
        ctx, tag=f"draft.{issue_id}", system=system, user=user, channel="doc", doc_id=DOC_ID
    )

    amended = extract_amended_claims(markdown) if strategy_value == "amend_claims" else {}
    lint_report: dict[str, Any] = {"errors": [], "findings": [], "repairs": 0}
    if amended:
        errors, findings = lint_amended_claims(base_claims, amended)
        repairs = 0
        while errors and repairs < MAX_CLAIMS_REPAIR:
            repairs += 1
            await ctx.emit(
                "log",
                {
                    "message": (
                        f"{issue_id} 修改后权项未通过权项 lint（第 {repairs} 次修复）："
                        + "；".join(str(e.get("message")) for e in errors[:2])
                    ),
                    "kind": "claims_lint",
                },
            )
            markdown = await build_service.stream_gen(
                ctx,
                tag=f"draft.{issue_id}.repair{repairs}",
                system=system,
                user=(
                    "上一版草稿中「修改后权利要求」未通过服务端权项校验，违规清单如下：\n"
                    + _lint_message(errors)
                    + "\n\n请修正这些权项后重新输出**完整**的本条答复 markdown"
                    "（保持【修改对照】与【修改后权利要求（全文）】结构不变）。\n\n"
                    + user
                ),
                channel="doc",
                doc_id=DOC_ID,
            )
            amended = extract_amended_claims(markdown)
            errors, findings = lint_amended_claims(base_claims, amended)
        lint_report = {
            "errors": errors,
            "findings": findings,
            "repairs": repairs,
            "claims": sorted(amended),
        }
    elif strategy_value == "amend_claims":
        lint_report["unresolved"] = ["草稿未输出可解析的「修改后权利要求（全文）」块"]

    return {
        "issue_id": issue_id,
        "order": order,
        "strategy": strategy_value,
        "statute": str(issue.get("statute") or ""),
        "defect_type": str(issue.get("defect_type") or ""),
        "claims_involved": list(issue.get("claims_involved") or []),
        "markdown": markdown.strip(),
        "amended_claims": {str(k): v for k, v in amended.items()},
        "claims_lint": lint_report,
        "cases_used": [c["case_id"] for c in cases],
    }


async def draft(ctx: Ctx) -> dict[str, Any]:
    """D4 逐条起草：每 issue 一次流式 GEN；amend_claims 额外过权项 lint。"""
    notice = dict(ctx.state.get("notice") or {})
    issues = list(notice.get("issues") or [])
    choices = {
        str(c.get("issue_id")): c for c in (ctx.state.get("strategy_choices") or [])
    }
    base_claims = {int(k): v for k, v in (ctx.state.get("base_claims") or {}).items()}

    drafts: list[dict[str, Any]] = []
    for position, issue in enumerate(issues, start=1):
        issue_id = str(issue.get("id") or "")
        await ctx.progress(
            "逐条撰写意见陈述",
            index=position, total=len(issues),
            detail=f"第 {position} 条：{str(issue.get('statute') or issue_id)}"[:60],
            waiting_for="模型",
        )
        choice = choices.get(issue_id) or {"strategy": "argue_only"}
        drafts.append(await _draft_issue(ctx, issue, choice, base_claims))

    amended_total = sum(len(d["amended_claims"]) for d in drafts)
    await ctx.emit(
        "log",
        {
            "message": (
                f"逐条起草完成：{len(drafts)} 条答复"
                + (f"，涉及 {amended_total} 项权利要求修改" if amended_total else "")
                + "。"
            )
        },
    )
    return {"drafts": drafts}


# ---------------------------------------------------------------------------
# 步骤 5：review_deliver —— 装配 + 跨条一致性 + 终稿人审 [PAUSE P3] + 交付
# ---------------------------------------------------------------------------


def _norm_ws(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def check_cross_issue(
    drafts: Sequence[Mapping[str, Any]], base_claims: Mapping[int, str]
) -> tuple[list[dict[str, Any]], dict[int, str]]:
    """跨条一致性确定性检查；返回 (发现清单, 合并后的修改权项)。

    查三件事：① 同一权项在多条 issue 下给出互不相同的修改文本；② 合并后的权项编号
    从 1 起连续；③ 合并后的整套权项过 claims_lint（引用基础、句号、多引不引多引）。
    """
    findings: list[dict[str, Any]] = []
    by_claim: dict[int, list[tuple[str, str]]] = {}
    for item in drafts:
        for raw_no, text in (item.get("amended_claims") or {}).items():
            by_claim.setdefault(int(raw_no), []).append((str(item.get("issue_id")), str(text)))

    merged: dict[int, str] = {}
    for no, entries in sorted(by_claim.items()):
        variants = {_norm_ws(t) for _, t in entries}
        if len(variants) > 1:
            findings.append(
                {
                    "severity": "error",
                    "claim_no": no,
                    "issue_ids": [i for i, _ in entries],
                    "message": (
                        f"权利要求{no} 在条目 {'、'.join(i for i, _ in entries)} 下给出了 "
                        f"{len(variants)} 份互不相同的修改文本，需合并为一份"
                    ),
                }
            )
        merged[no] = entries[-1][1]

    if merged:
        full = {int(k): str(v) for k, v in base_claims.items()}
        full.update(merged)
        numbers = sorted(full)
        if numbers and numbers != list(range(1, len(numbers) + 1)):
            findings.append(
                {
                    "severity": "error",
                    "claim_no": None,
                    "issue_ids": [],
                    "message": (
                        "合并后的权利要求编号不连续："
                        + "、".join(str(n) for n in numbers)
                        + "；删项后须重新编号并同步修正引用关系"
                    ),
                }
            )
        for finding in claims_lint.lint_claims([full[n] for n in numbers], ""):
            if finding.get("severity") != "error":
                continue
            no = int(finding.get("claim_no") or 0)
            findings.append(
                {
                    "severity": "error",
                    "claim_no": no,
                    "issue_ids": [i for i, _ in by_claim.get(no, [])],
                    "message": f"合并后权项校验：{finding.get('message')}",
                }
            )
    return findings, merged


def assemble_response(
    notice: Mapping[str, Any],
    drafts: Sequence[Mapping[str, Any]],
    base_claims: Mapping[int, str],
    merged_claims: Mapping[int, str],
    unresolved: Sequence[str] = (),
) -> str:
    """确定性装配意见陈述书（答复期限头 + 逐条答复 + 权项修改替换页）。"""
    kind = str(notice.get("notice_kind") or "office_action")
    label = {
        "office_action": "审查意见通知书",
        "correction": "补正通知书",
        "rejection": "驳回决定",
    }.get(kind, "通知书")
    if kind == "office_action" and notice.get("notice_no"):
        label = f"第{notice['notice_no']}次{label}"

    lines: list[str] = [f"# 意见陈述书（{DRAFT_BANNER}）", ""]
    lines.append(f"**申请号**：{notice.get('application_no') or '（待填写）'}")
    lines.append(f"**通知书**：{label}")
    ptype = str(notice.get("patent_type") or "")
    if ptype:
        lines.append(
            "**专利类型**："
            + {"invention": "发明", "utility_model": "实用新型", "design": "外观设计"}.get(ptype, ptype)
        )
    lines.append(f"**答复期限**：{notice.get('deadline') or '（通知书未载明，请按发文日自行核算）'}")
    lines.append("")
    lines.append(
        "> 本文件为平台生成的**答复草稿**，须代理人/发明人复核并签字后方可递交，"
        "平台不代替专利代理签字与正式递交。"
    )
    lines.append("")
    lines.append("---")
    lines.append("")

    lines.append("## 一、答复概要")
    lines.append("")
    lines.append("| 条目 | 法条 | 缺陷类型 | 涉及权利要求 | 采用策略 |")
    lines.append("|------|------|----------|--------------|----------|")
    for item in drafts:
        claims = item.get("claims_involved") or []
        claims_text = "、".join(str(c) for c in claims) if claims else "说明书/摘要"
        defect = str(item.get("defect_type") or "")
        strategy_value = str(item.get("strategy") or "")
        lines.append(
            f"| 第{item.get('order')}条（{item.get('issue_id')}） "
            f"| {item.get('statute') or '未明示'} "
            f"| {DEFECT_TYPE_LABEL.get(defect, defect)} "
            f"| {claims_text} "
            f"| {RESPONSE_STRATEGY_LABEL.get(strategy_value, strategy_value)} |"
        )
    lines.append("")

    lines.append("## 二、逐条答复意见")
    lines.append("")
    for item in drafts:
        body = str(item.get("markdown") or "").strip()
        if body:
            lines.append(body)
            lines.append("")

    if merged_claims:
        lines.append("## 三、权利要求书修改替换页")
        lines.append("")
        lines.append(
            "以下为修改后的权利要求书全文（未列出的权项维持原文；修改依据见各条答复的【修改对照】）："
        )
        lines.append("")
        full = {int(k): str(v) for k, v in base_claims.items()}
        full.update({int(k): str(v) for k, v in merged_claims.items()})
        for no in sorted(full):
            marker = " <!-- 本次修改 -->" if no in {int(k) for k in merged_claims} else ""
            lines.append(f"{full[no]}{marker}")
            lines.append("")

    if unresolved:
        lines.append("## 附：待人工确认事项（草稿附注，递交前请处理并删除本节）")
        lines.append("")
        lines.extend(f"- {u}" for u in unresolved)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


async def _audit_consistency(
    ctx: Ctx, document: str, drafts: Sequence[Mapping[str, Any]], code_findings: Sequence[Any]
) -> ConsistencyAudit:
    """一次 AUDIT 复核跨条一致性（在确定性检查之上兜口径问题）。"""
    system = assembler.assemble(
        AUDIT_PARTS,
        runtime_ctx={
            "case_card": _case_card(ctx),
            "extra": {
                "code_findings": list(code_findings),
                "issues": [
                    {
                        "issue_id": d.get("issue_id"),
                        "strategy": d.get("strategy"),
                        "amended_claims": sorted(int(k) for k in (d.get("amended_claims") or {})),
                    }
                    for d in drafts
                ],
            },
        },
    )
    user = (
        "以下为装配后的意见陈述书草稿。请只做**跨条一致性**复核并输出一个 JSON 对象："
        '{"verdict":"pass|revise","findings":[{"severity":"error|warning|info","message":"",'
        '"issue_ids":[],"claim_no":null}],"notes":""}。\n'
        "重点：同一权利要求在多条答复下的修改是否冲突；权项重编号与引用关系是否连续自洽；"
        "各条陈述的事实认定（区别特征、对比文件公开内容）是否互相矛盾；"
        "是否出现未经检索却声称引用历史案例的表述。不要输出修订后全文。\n\n"
        "===== 意见陈述书草稿 =====\n" + document
    )
    return await build_service.call_struct(
        ctx, ConsistencyAudit, tag="consistency", system=system, user=user, call_class="AUDIT"
    )


def _export_docx_sync(markdown: str, filename_stem: str) -> tuple[bytes, dict[str, Any]]:
    """md → docx（走 services/export_docx 的子进程管线）。"""
    cfg = get_config()
    workdir = cfg.tmp_dir / f"oa_{ULID()}"
    workdir.mkdir(parents=True, exist_ok=True)
    md_path = workdir / f"{filename_stem}.md"
    docx_path = workdir / f"{filename_stem}.docx"
    try:
        md_path.write_text(markdown, encoding="utf-8")
        stats = export_docx_service.export_md_to_docx_sync(md_path, docx_path)
        return docx_path.read_bytes(), stats
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def _deliver_files(ctx: Ctx, markdown: str, title: str, with_docx: bool) -> dict[str, Any]:
    """落 oa_response_md（+ oa_response_docx）交付物。"""
    md_artifact = await artifacts_service.save_artifact(
        ctx.case_id,
        "oa_response_md",
        markdown,
        "md",
        title=title or None,
        md_text="",
        run_group=ctx.run_group,
        iteration_type="initial",
        summary=f"意见陈述书（{DRAFT_BANNER}）",
    )
    await _emit_artifact(ctx.case_id, md_artifact)
    downloads = [
        {"kind": md_artifact.kind, "artifact_id": md_artifact.id, "filename": md_artifact.filename}
    ]

    docx_artifact = None
    docx_error: str | None = None
    docx_stats: dict[str, Any] = {}
    if with_docx:
        stem = artifacts_service.strip_timestamp(md_artifact.filename)
        try:
            docx_bytes, docx_stats = await anyio.to_thread.run_sync(
                lambda: _export_docx_sync(markdown, stem)
            )
        except export_docx_service.DocxExportError as exc:
            docx_error = str(exc)
            logger.warning("意见陈述书 docx 导出失败：%s", exc)
        else:
            docx_artifact = await artifacts_service.save_artifact(
                ctx.case_id,
                "oa_response_docx",
                docx_bytes,
                "docx",
                title=title or None,
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

    await _emit_doc_version(ctx.case_id, md_artifact, downloads)
    return {
        "md": json.loads(md_artifact.model_dump_json()),
        "docx": json.loads(docx_artifact.model_dump_json()) if docx_artifact else None,
        "docx_error": docx_error,
        "docx_stats": docx_stats,
        "downloads": downloads,
    }


async def _archive_to_library(ctx: Ctx, document: str) -> dict[str, Any]:
    """归档回案例库：以本案材料生成 `draft` 状态的案例笔记（闭环，人审后才入检索库）。"""
    materials_state = ctx.state.get("materials") or {}
    notice = dict(ctx.state.get("notice") or {})
    choices = ctx.state.get("strategy_choices") or []
    strategies = [
        RESPONSE_TO_CASE_STRATEGY.get(str(c.get("strategy")), "other") for c in choices
    ]
    materials = [
        {
            "name": str(materials_state.get("notice_name") or "通知书"),
            "role": "审查意见通知书",
            "text": str(materials_state.get("notice_text") or ""),
        },
        {"name": "意见陈述书草稿.md", "role": "本案答复", "text": document},
    ]
    result = await oa_library.ingest(
        materials,
        notes=(
            "由审查意见答复流水线自动归档；本案采用策略："
            + "、".join(dict.fromkeys(strategies))
            + f"；申请号 {notice.get('application_no') or '（脱敏）'}。"
        ),
        outcome="pending",
        status="draft",
        user_id=str((ctx.case or {}).get("user_id") or ""),
    )
    item = result["item"]
    await ctx.emit(
        "log",
        {
            "message": (
                f"已归档回案例库（草稿）：{item.title or item.case_id}；"
                "请到「OA 案例库」核对 frontmatter 与脱敏项后确认入库。"
            )
        },
    )
    return {
        "library_id": item.id,
        "case_id": item.case_id,
        "title": item.title,
        "status": item.status,
        "redactions": result["redactions"],
        "normalized": result["normalized"],
    }


async def review_deliver(ctx: Ctx) -> dict[str, Any]:
    """D5 汇总：装配 → 跨条一致性（代码 + AUDIT）→ [PAUSE P3] 终稿人审 → 交付/归档。"""
    notice = dict(ctx.state.get("notice") or {})
    drafts = list(ctx.state.get("drafts") or [])
    base_claims = {int(k): v for k, v in (ctx.state.get("base_claims") or {}).items()}
    if not drafts:
        raise ValueError("没有可汇总的逐条答复草稿")

    code_findings, merged_claims = check_cross_issue(drafts, base_claims)
    unresolved = [str(f["message"]) for f in code_findings]
    document = assemble_response(notice, drafts, base_claims, merged_claims, unresolved)

    audit = await _audit_consistency(ctx, document, drafts, code_findings)
    audit_problems = [
        f"{f.severity}｜{f.message}" for f in audit.findings if f.severity in ("error", "warning")
    ]
    all_unresolved = list(dict.fromkeys([*unresolved, *audit_problems]))
    if audit_problems:
        document = assemble_response(notice, drafts, base_claims, merged_claims, all_unresolved)

    await ctx.emit(
        "log",
        {
            "message": (
                f"跨条一致性检查完成：确定性检查 {len(code_findings)} 项、"
                f"AUDIT 复核 {len(audit.findings)} 项（verdict={audit.verdict}）。"
            ),
            "kind": "consistency",
        },
    )

    prompt = human_gate_prompt()
    if all_unresolved:
        prompt += "\n\n平台检出以下待人工确认事项：\n" + "\n".join(
            f"- {u}" for u in all_unresolved[:8]
        )
    answer = _answer(
        await ctx.await_user(
            InteractionRequest(
                kind="confirm",
                schema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "title": "处置",
                            "enum": ["confirm", "revise", "hold"],
                            "enumNames": ["确认仅作内部草稿（落盘交付）", "需要修改策略", "暂停"],
                            "default": "confirm",
                        },
                        "archive": {
                            "type": "boolean",
                            "title": "归档回案例库（生成待人审的案例笔记草稿）",
                            "default": False,
                        },
                        "note": {"type": "string", "title": "复核意见（留档）"},
                    },
                },
                prompt=prompt,
                default={
                    "action": "confirm",
                    "archive": False,
                    "note": "",
                    "document": document,
                    "unresolved": all_unresolved,
                    "consistency": {
                        "code_findings": code_findings,
                        "audit": audit.model_dump(),
                    },
                },
            )
        )
    )

    action = str(answer.get("action") or "confirm")
    confirmed = action == "confirm"
    title = str((ctx.case or {}).get("title") or "意见陈述书")
    files = await _deliver_files(ctx, document, title, with_docx=confirmed)

    archive: dict[str, Any] | None = None
    if confirmed and answer.get("archive"):
        try:
            archive = await _archive_to_library(ctx, document)
        except Exception as exc:  # noqa: BLE001 —— 归档失败不影响已交付的答复
            logger.warning("归档回案例库失败：%s", exc)
            archive = {"error": str(exc)}

    lines = [f"意见陈述书已{'交付' if confirmed else '暂存为草稿'}："]
    lines.append(f"- Markdown：{files['md']['filename']}")
    if files.get("docx"):
        lines.append(f"- Word 文件：{files['docx']['filename']}")
    elif files.get("docx_error"):
        lines.append(f"- Word 文件未能生成：{files['docx_error']}")
    elif not confirmed:
        lines.append("- 未确认为内部草稿，暂不生成 Word 文件；处理完复核意见后可重跑本步。")
    if archive and archive.get("library_id"):
        lines.append(f"- 已归档回案例库（草稿）：{archive.get('title') or archive.get('case_id')}")
    if all_unresolved:
        lines.append(f"- 待人工确认 {len(all_unresolved)} 项：")
        lines.extend(f"  · {u}" for u in all_unresolved[:5])
    await ctx.chat_delta("\n".join(lines))
    await ctx.chat_done()

    return {
        "response_markdown": document,
        "merged_claims": {str(k): v for k, v in merged_claims.items()},
        "consistency": {
            "code_findings": code_findings,
            "audit": audit.model_dump(),
            "unresolved": all_unresolved,
        },
        "review": {"action": action, "confirmed": confirmed, "note": str(answer.get("note") or "")},
        "deliver": {"files": files, "archive": archive},
    }


# ---------------------------------------------------------------------------
# 步骤表
# ---------------------------------------------------------------------------


def build_steps(case: dict[str, Any]) -> list[StepDef]:
    """模块 D 的五步流水线（D1–D5）。"""
    return [
        StepDef(key="notice_extract", name_zh="通知书结构化", handler=notice_extract, gate="oa_issues"),
        StepDef(key="retrieve", name_zh="案例检索", handler=retrieve, gate="oa_retrieve"),
        StepDef(key="strategy", name_zh="策略规划", handler=strategy, gate="oa_strategy"),
        StepDef(key="draft", name_zh="逐条起草", handler=draft),
        StepDef(key="review_deliver", name_zh="汇总与交付", handler=review_deliver, gate="confirm"),
    ]
