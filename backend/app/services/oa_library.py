"""OA 案例库 CRUD（模块 D 的 RAG 语料层）。

流程（ingest_case.md + guardrails.md 暂停点 P4）：

    材料（PDF/md/docx…）→ (srv) 转 markdown（复用 services/convert）
      → (llm:STRUCT) 案例笔记草稿 + frontmatter + 脱敏登记 → **status='draft'**
      → 人审（前端 OACasesPage）→ PUT {status:'confirmed'}
      → (srv) 按笔记小节切块（每块 300–600 字）+ 向量化（services/vector）

硬性纪律：
- **只有 `confirmed` 的案例进检索库**（oa_case.schema.yaml「硬性规则」第 2 条）；
  `draft` 只落库不切块、不向量化；
- 枚举（statutes / defect_types / patent_type / notice_kind / outcome / strategy）
  以 `references/schemas/oa_case.schema.yaml` 为基准，服务端归一化，非法值丢弃并
  在 `normalized` 里如实告知，不静默改写；
- 脱敏登记 `redactions` 随案例保存，供人审逐项确认（`original_hint` 只写类别）。

frontmatter_json 里以 `_` 开头的键为平台元数据（脱敏登记、推断字段、嵌入错误、
材料文件名），对外 API 的 `frontmatter` 字段会剥掉它们。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ulid import ULID

from ..config import get_config
from ..db import database as db
from ..models.oa import (
    CASE_STRATEGIES,
    DEFECT_TYPES,
    LIBRARY_STATUS_ALIASES,
    NOTICE_KINDS,
    OUTCOMES,
    PATENT_TYPES,
    RESPONSE_TO_CASE_STRATEGY,
    OaCaseDraft,
    OaCaseFrontmatter,
    OaLibraryDetail,
    OaLibraryItem,
    Redaction,
    validate_statute,
)
from . import assembler, convert as convert_service, llm, vector

logger = logging.getLogger(__name__)

#: 案例库材料的落盘目录（data/uploads/_oa_library/）
LIBRARY_UPLOAD_KEY = "_oa_library"

#: ingest 的 prompt 装配清单（§0.2：平台角色头 → 模块总则 → 脱敏 → 阶段指令 → 模板）
INGEST_PARTS = [
    "common/system_base",
    "oa/guardrails",
    "common/desensitization",
    "oa/ingest_case",
    "oa/case_note_template",
]

#: 单份材料注入 prompt 的字符上限（上下文经济，§3.6）
MATERIAL_CHARS = 12000

_SLUG_RE = re.compile(r"[^a-z0-9\-]+")


# ---------------------------------------------------------------------------
# 材料读取（复用 services/convert 的 PDF→md 管线）
# ---------------------------------------------------------------------------


def read_material_sync(orig_name: str, payload: bytes) -> dict[str, Any]:
    """把上传的案例材料落盘并转 markdown；返回 `{name, text, path, error}`。"""
    cfg = get_config()
    case_dir = cfg.uploads_dir / LIBRARY_UPLOAD_KEY
    case_dir.mkdir(parents=True, exist_ok=True)
    safe = convert_service.sanitize_filename(orig_name or "case")
    stored = convert_service.allocate_upload_path(case_dir, safe)
    stored.write_bytes(payload)

    result = convert_service.convert_upload(LIBRARY_UPLOAD_KEY, stored)
    text = ""
    if result.md_path is not None:
        try:
            text = Path(result.md_path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:  # pragma: no cover —— 刚写出的文件几乎不会读不到
            result.meta.setdefault("convert_error", f"读取转换产物失败：{exc}")
    return {
        "name": safe,
        "text": text,
        "path": str(stored),
        "error": str(result.meta.get("convert_error") or ""),
    }


# ---------------------------------------------------------------------------
# frontmatter 归一化
# ---------------------------------------------------------------------------


def slugify(value: str, fallback: str = "oa-case") -> str:
    """英文小写 slug（中文等非 ASCII 字符会被剔除，全空时用 fallback）。"""
    text = (value or "").strip().lower().replace(" ", "-").replace("_", "-")
    text = _SLUG_RE.sub("-", text).strip("-")
    text = re.sub(r"-{2,}", "-", text)
    return text[:64] or fallback


def normalize_frontmatter(
    data: Mapping[str, Any] | OaCaseFrontmatter,
    *,
    status: str | None = None,
) -> tuple[OaCaseFrontmatter, list[str]]:
    """枚举归一化 + 缺省补全；返回 (frontmatter, 归一化说明列表)。"""
    raw = data.model_dump() if isinstance(data, OaCaseFrontmatter) else dict(data or {})
    fm = OaCaseFrontmatter.model_validate(raw)
    notes: list[str] = []

    if fm.patent_type not in PATENT_TYPES:
        notes.append(f"patent_type「{fm.patent_type}」不在枚举内，已归一化为 invention")
        fm.patent_type = "invention"
    if fm.notice_kind not in NOTICE_KINDS:
        notes.append(f"notice_kind「{fm.notice_kind}」不在枚举内，已归一化为 other")
        fm.notice_kind = "other"
    if fm.outcome not in OUTCOMES:
        notes.append(f"outcome「{fm.outcome}」不在枚举内，已归一化为 unknown")
        fm.outcome = "unknown"

    defects: list[str] = []
    for value in fm.defect_types:
        value = str(value).strip()
        if value in DEFECT_TYPES:
            if value not in defects:
                defects.append(value)
        elif value:
            notes.append(f"defect_type「{value}」不在枚举内，已丢弃")
    fm.defect_types = defects

    strategies: list[str] = []
    for value in fm.strategy:
        value = str(value).strip()
        mapped = value if value in CASE_STRATEGIES else RESPONSE_TO_CASE_STRATEGY.get(value, "")
        if mapped:
            if mapped != value:
                notes.append(f"strategy「{value}」已按案例库枚举归一化为「{mapped}」")
            if mapped not in strategies:
                strategies.append(mapped)
        elif value:
            notes.append(f"strategy「{value}」不在枚举内，已丢弃")
    fm.strategy = strategies

    statutes: list[str] = []
    for value in fm.statutes:
        value = str(value).strip()
        err = validate_statute(value)
        if err:
            notes.append(f"statutes 中的「{value}」不是合法条款表述，已丢弃")
            continue
        if value not in statutes:
            statutes.append(value)
    fm.statutes = statutes

    fm.case_id = slugify(fm.case_id or fm.title or "oa-case")
    if not fm.title.strip():
        fm.title = fm.case_id
    resolved = status or LIBRARY_STATUS_ALIASES.get(fm.status, "draft")
    fm.status = resolved
    fm.redacted = bool(fm.redacted)
    fm.tags = [str(t).strip() for t in fm.tags if str(t).strip()]
    return fm, notes


def render_note(frontmatter: Mapping[str, Any] | OaCaseFrontmatter, note_md: str) -> str:
    """frontmatter + 正文 → 完整案例笔记 markdown（YAML front-matter 包裹）。"""
    data = (
        frontmatter.model_dump()
        if isinstance(frontmatter, OaCaseFrontmatter)
        else dict(frontmatter or {})
    )
    public = {k: v for k, v in data.items() if not str(k).startswith("_")}
    lines = ["---"]
    for key, value in public.items():
        if isinstance(value, list):
            if not value:
                lines.append(f"{key}: []")
            else:
                lines.append(f"{key}:")
                lines.extend(f"  - {item}" for item in value)
        elif isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        else:
            text = str(value if value is not None else "")
            lines.append(f'{key}: "{text}"' if (":" in text or text == "") else f"{key}: {text}")
    lines.append("---")
    body = (note_md or "").strip()
    return "\n".join(lines) + "\n\n" + body + "\n"


# ---------------------------------------------------------------------------
# 行 ↔ 契约
# ---------------------------------------------------------------------------


def _parse_frontmatter_json(text: str | None) -> dict[str, Any]:
    try:
        data = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _public_frontmatter(stored: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in stored.items() if not str(k).startswith("_")}


def _chunk_count_sync(library_id: str) -> int:
    row = db.query_one("SELECT COUNT(*) AS n FROM oa_chunks WHERE library_id=?", (library_id,))
    return int(row["n"]) if row else 0


def _chunks_sync(library_id: str) -> list[str]:
    rows = db.query_all(
        "SELECT text FROM oa_chunks WHERE library_id=? ORDER BY chunk_index ASC", (library_id,)
    )
    return [str(r["text"]) for r in rows]


def _row_to_item(row: Mapping[str, Any], chunk_count: int = 0) -> OaLibraryItem:
    stored = _parse_frontmatter_json(row["frontmatter_json"])
    fm = _public_frontmatter(stored)
    return OaLibraryItem(
        id=str(row["id"]),
        case_id=str(fm.get("case_id") or ""),
        title=str(fm.get("title") or ""),
        status=str(row["status"]),  # type: ignore[arg-type]
        embedded=bool(row["embedded"]),
        chunk_count=chunk_count,
        patent_type=str(fm.get("patent_type") or ""),
        statutes=[str(x) for x in (fm.get("statutes") or [])],
        defect_types=[str(x) for x in (fm.get("defect_types") or [])],
        domain=str(fm.get("domain") or ""),
        outcome=str(fm.get("outcome") or ""),
        tags=[str(x) for x in (fm.get("tags") or [])],
        frontmatter=fm,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _row_to_detail(row: Mapping[str, Any]) -> OaLibraryDetail:
    stored = _parse_frontmatter_json(row["frontmatter_json"])
    fm = _public_frontmatter(stored)
    library_id = str(row["id"])
    item = _row_to_item(row, _chunk_count_sync(library_id))
    return OaLibraryDetail(
        **item.model_dump(),
        note_md=str(row["case_note_md"] or ""),
        note_full_md=render_note(fm, str(row["case_note_md"] or "")),
        redactions=[Redaction.model_validate(r) for r in (stored.get("_redactions") or [])],
        inferred_fields=[str(x) for x in (stored.get("_inferred_fields") or [])],
        chunks=_chunks_sync(library_id),
        embed_error=str(stored.get("_embed_error") or ""),
    )


def visible_to(row: Mapping[str, Any], viewer: Mapping[str, Any] | None) -> bool:
    """案例对该主体是否可见（M8 数据隔离）。

    - `viewer=None`：服务层内部调用（流水线、重建），不做过滤；
    - 管理员：全部可见；
    - 普通用户：自己的 + `visibility='shared'` 的（管理员可把优质案例标为共享）。
    """
    if viewer is None:
        return True
    if str(viewer.get("role") or "user") == "admin":
        return True
    data = dict(row)   # sqlite3.Row 没有 .get()
    if str(data.get("visibility") or "private") == "shared":
        return True
    return str(data.get("user_id") or "") == str(viewer.get("id") or "\0")


def _get_row_sync(library_id: str, viewer: Mapping[str, Any] | None = None) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM oa_library WHERE id=?", (library_id,))
    if row is None:
        raise KeyError(f"案例不存在：{library_id}")
    data = dict(row)
    if not visible_to(data, viewer):
        # 不泄露「存在但不属于你」——与 resolve_case 同口径
        raise KeyError(f"案例不存在：{library_id}")
    return data


def get_sync(library_id: str, viewer: Mapping[str, Any] | None = None) -> OaLibraryDetail:
    """读单个案例详情（同步）。"""
    return _row_to_detail(_get_row_sync(library_id, viewer))


async def get(library_id: str, viewer: Mapping[str, Any] | None = None) -> OaLibraryDetail:
    """读单个案例详情。"""
    return await db.arun(get_sync, library_id, viewer)


# ---------------------------------------------------------------------------
# 列表
# ---------------------------------------------------------------------------


def list_sync(
    *,
    tag: str | None = None,
    defect_type: str | None = None,
    status: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
    viewer: Mapping[str, Any] | None = None,
) -> tuple[list[OaLibraryItem], int]:
    """案例库列表（tag / defect_type / status 过滤 + 关键词模糊匹配）。

    `viewer` 给定时按属主 + visibility 过滤（None = 服务层内部调用，不过滤）。
    """
    rows = db.query_all("SELECT * FROM oa_library ORDER BY updated_at DESC, created_at DESC")
    keyword = (q or "").strip().lower()
    picked: list[Mapping[str, Any]] = []
    for row in rows:
        if not visible_to(row, viewer):
            continue
        stored = _parse_frontmatter_json(row["frontmatter_json"])
        fm = _public_frontmatter(stored)
        if status and str(row["status"]) != status:
            continue
        if tag and tag not in {str(x) for x in (fm.get("tags") or [])}:
            continue
        if defect_type and defect_type not in {str(x) for x in (fm.get("defect_types") or [])}:
            continue
        if keyword:
            haystack = " ".join(
                [
                    str(fm.get("title") or ""), str(fm.get("case_id") or ""),
                    str(fm.get("domain") or ""),
                    " ".join(str(x) for x in (fm.get("statutes") or [])),
                    " ".join(str(x) for x in (fm.get("tags") or [])),
                    str(row["case_note_md"] or ""),
                ]
            ).lower()
            if keyword not in haystack:
                continue
        picked.append(row)
    total = len(picked)
    window = picked[max(0, offset) : max(0, offset) + max(1, limit)]
    return [_row_to_item(r, _chunk_count_sync(str(r["id"]))) for r in window], total


async def list_items(**kwargs: Any) -> tuple[list[OaLibraryItem], int]:
    """list_sync 的异步包装。"""
    return await db.arun(lambda: list_sync(**kwargs))


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------


def create_sync(
    frontmatter: Mapping[str, Any] | OaCaseFrontmatter,
    note_md: str,
    *,
    status: str = "draft",
    redactions: Sequence[Mapping[str, Any]] | None = None,
    inferred_fields: Sequence[str] | None = None,
    materials: Sequence[str] | None = None,
    user_id: str = "",
    visibility: str = "private",
) -> tuple[str, list[str]]:
    """插入一条案例（默认 draft、private）；返回 (library_id, 归一化说明)。"""
    status = status if status in ("draft", "confirmed") else "draft"
    fm, notes = normalize_frontmatter(frontmatter, status=status)
    now = db.now_str()
    fm.created_at = fm.created_at or now
    fm.updated_at = now

    stored = fm.model_dump()
    stored["_redactions"] = [dict(r) for r in (redactions or [])]
    stored["_inferred_fields"] = [str(x) for x in (inferred_fields or [])]
    stored["_materials"] = [str(x) for x in (materials or [])]

    library_id = str(ULID())
    db.execute(
        """
        INSERT INTO oa_library(id, case_note_md, frontmatter_json, status, embedded,
                               user_id, visibility, created_at, updated_at)
        VALUES (?,?,?,?,0,?,?,?,?)
        """,
        (library_id, note_md or "", json.dumps(stored, ensure_ascii=False), status,
         user_id or "", visibility if visibility in ("private", "shared") else "private",
         now, now),
    )
    return library_id, notes


def _update_row_sync(
    library_id: str,
    *,
    note_md: str | None = None,
    frontmatter: Mapping[str, Any] | None = None,
    status: str | None = None,
) -> list[str]:
    """更新案例行（不触发切块/嵌入）；返回归一化说明。"""
    row = _get_row_sync(library_id)
    stored = _parse_frontmatter_json(row["frontmatter_json"])
    meta = {k: v for k, v in stored.items() if str(k).startswith("_")}
    public = _public_frontmatter(stored)
    if frontmatter:
        public.update({k: v for k, v in dict(frontmatter).items() if not str(k).startswith("_")})
    resolved_status = status or str(row["status"])
    fm, notes = normalize_frontmatter(public, status=resolved_status)
    fm.updated_at = db.now_str()
    merged = {**fm.model_dump(), **meta}

    def op(conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE oa_library SET case_note_md=?, frontmatter_json=?, status=?, updated_at=? "
            "WHERE id=?",
            (
                row["case_note_md"] if note_md is None else note_md,
                json.dumps(merged, ensure_ascii=False),
                resolved_status,
                fm.updated_at,
                library_id,
            ),
        )

    db.transaction(op)
    return notes


def _set_embed_error_sync(library_id: str, message: str) -> None:
    row = _get_row_sync(library_id)
    stored = _parse_frontmatter_json(row["frontmatter_json"])
    stored["_embed_error"] = message
    db.execute(
        "UPDATE oa_library SET frontmatter_json=?, updated_at=? WHERE id=?",
        (json.dumps(stored, ensure_ascii=False), db.now_str(), library_id),
    )


async def reindex(library_id: str) -> dict[str, Any]:
    """按当前 status 同步检索层（confirmed → 切块+向量化；draft → 清空检索块）。

    嵌入失败不抛：案例仍以关键词模式可检索，错误记在 `_embed_error` 供前端提示。
    """
    try:
        result = await vector.upsert(library_id)
    except Exception as exc:  # noqa: BLE001 —— 嵌入失败降级为关键词可检索
        logger.warning("案例 %s 向量化失败：%s", library_id, exc)
        await db.arun(_set_embed_error_sync, library_id, str(exc))
        return {"library_id": library_id, "chunks": 0, "embedded": 0,
                "mode": "keyword", "error": str(exc)}
    await db.arun(_set_embed_error_sync, library_id, "")
    return result


async def create(
    frontmatter: Mapping[str, Any] | OaCaseFrontmatter,
    note_md: str,
    *,
    status: str = "draft",
    **kwargs: Any,
) -> tuple[OaLibraryDetail, list[str]]:
    """新建案例；`status='confirmed'` 时立即切块并向量化。"""
    library_id, notes = await db.arun(
        lambda: create_sync(frontmatter, note_md, status=status, **kwargs)
    )
    if status == "confirmed":
        await reindex(library_id)
    return await get(library_id), notes


async def update(
    library_id: str,
    *,
    status: str | None = None,
    note_md: str | None = None,
    frontmatter: Mapping[str, Any] | None = None,
    viewer: Mapping[str, Any] | None = None,
) -> tuple[OaLibraryDetail, list[str]]:
    """更新案例；status 或正文变化时同步检索层（confirmed 才切块+嵌入）。"""
    before = await db.arun(_get_row_sync, library_id, viewer)
    notes = await db.arun(
        lambda: _update_row_sync(
            library_id, note_md=note_md, frontmatter=frontmatter, status=status
        )
    )
    changed_status = status is not None and status != str(before["status"])
    changed_note = note_md is not None and note_md != str(before["case_note_md"] or "")
    if changed_status or changed_note:
        await reindex(library_id)
    return await get(library_id, viewer), notes


def delete_sync(library_id: str, viewer: Mapping[str, Any] | None = None) -> None:
    """删除案例（连带 chunks 与向量）。"""
    _get_row_sync(library_id, viewer)
    vector.delete_sync(library_id)
    db.execute("DELETE FROM oa_library WHERE id=?", (library_id,))


async def delete(library_id: str, viewer: Mapping[str, Any] | None = None) -> None:
    """delete_sync 的异步包装。"""
    await db.arun(delete_sync, library_id, viewer)


# ---------------------------------------------------------------------------
# ingest：材料 → LLM 案例笔记草稿 → draft 行
# ---------------------------------------------------------------------------


def _materials_block(materials: Sequence[Mapping[str, Any]]) -> str:
    parts: list[str] = []
    for item in materials:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        name = str(item.get("name") or "材料")
        role = str(item.get("role") or "")
        head = f"===== 材料《{name}》" + (f"（{role}）" if role else "") + " ====="
        parts.append(head + "\n" + text[:MATERIAL_CHARS])
    return "\n\n".join(parts)


async def ingest(
    materials: Sequence[Mapping[str, Any]],
    *,
    notes: str = "",
    outcome: str = "",
    source_paths: Sequence[str] | None = None,
    status: str = "draft",
    case_id: str | None = None,
    user_id: str = "",
) -> dict[str, Any]:
    """材料 → (llm:STRUCT) 案例笔记草稿 → 落 `status='draft'` 的案例行。

    `materials`：`[{name, text, role?}]`（通知书 / 意见陈述 / 结案信息…）。
    返回 `{item, redactions, inferred_fields, normalized, materials}`。
    """
    body = _materials_block(materials)
    if not body.strip():
        raise ValueError("没有可用于生成案例笔记的材料文本（转换失败或内容为空）")

    system = assembler.assemble(
        INGEST_PARTS,
        runtime_ctx={
            "extra": {
                "user_notes": notes,
                "closure_outcome": outcome,
                "material_files": [str(m.get("name") or "") for m in materials],
            }
        },
    )
    user = (
        "以下为待入库的历史案例材料。请按输出契约生成脱敏后的案例笔记草稿，"
        "只输出一个 JSON 对象。\n\n" + body
    )
    draft: OaCaseDraft = await llm.structured(
        [{"role": "system", "content": str(system)}, {"role": "user", "content": user}],
        OaCaseDraft,
        step_key="oa_library.ingest",
        temperature=0.1,
        user_id=user_id,
    )

    fm_data = draft.frontmatter.model_dump()
    if case_id:
        fm_data["case_id"] = case_id
    if outcome:
        fm_data.setdefault("outcome", outcome)
        if outcome in OUTCOMES:
            fm_data["outcome"] = outcome
    fm_data["source_paths"] = [str(p) for p in (source_paths or [])]

    item, normalized = await create(
        fm_data,
        draft.note_md,
        status=status,
        redactions=[r.model_dump() for r in draft.redactions],
        inferred_fields=list(draft.inferred_fields),
        materials=[str(m.get("name") or "") for m in materials],
        user_id=user_id,
    )
    return {
        "item": item,
        "redactions": [r.model_dump() for r in draft.redactions],
        "inferred_fields": list(draft.inferred_fields),
        "normalized": normalized,
        "materials": [str(m.get("name") or "") for m in materials],
    }


async def rebuild() -> dict[str, Any]:
    """全量重建向量库（维度变更 / 换模型后调用）。"""
    return await vector.rebuild()
