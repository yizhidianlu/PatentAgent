"""修订记录（prompt-porting-spec §3.4「revision_log 五要素」+「交底书修订对话记录.md 由 DB 导出」）。

五要素 = **记录时间 / 类型 / 用户说明摘要 / 交付文件 / 摘要摘录**。

存储策略（不新增表，沿用 §2 既有 DDL）：

- **每轮迭代**：往 `messages` 落一行 `role='system'`、`step_key='revision_log'` 的记录行，
  `content` 为记录 JSON，`meta_json.event='revision_log'` —— 与 SSE 重放源同表，但
  **不经 hub 广播**（`revision_log` 不在 SSE 事件契约里，避免污染前端事件流）；
- **初稿**：主流水线 `deliver` 不落记录行，读取时由 `artifacts`（kind=disclosure_md，
  iteration_type=initial）**合成**一条「初稿」记录，保证记录列表自成完整历史；
- **导出**：`export_log()` 把累计记录渲染成「交底书修订对话记录.md」，落 `revision_log_md`
  交付物 —— 每轮重写产出**新版本**（artifacts 只增不改），内容为**累计追加**。

本模块只做记录与渲染：不发 SSE、不调 LLM，便于单测。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from ulid import ULID

from ..db import database as db
from ..models.artifact import ArtifactOut
from . import artifacts as artifacts_service

logger = logging.getLogger(__name__)

# messages 行的定位键（step_key）与事件名（meta_json.event）
RECORD_STEP_KEY = "revision_log"
RECORD_EVENT = "revision_log"

# 五要素中两段文本的截断长度
INSTRUCTION_CHARS = 240
SUMMARY_CHARS = 600

# 导出文件的案件名（artifacts 命名器据此生成 {名称}_{时间戳}.md）
LOG_TITLE = "交底书修订对话记录"

# 迭代类型 → 中文标签（与前端版本下拉的标签口径一致：初稿/合并迭代/纠正迭代）
TYPE_LABEL: dict[str, str] = {
    "initial": "初稿",
    "merge": "合并迭代",
    "correction": "纠正迭代",
    "rebuild": "偏向点强化",
    "export": "格式导出",
}

# 交底书正文类交付物（记录「交付文件」一栏时列出）
DOC_KINDS = ("disclosure_md", "disclosure_docx", "disclosure_pdf")


class RevisionRecord(BaseModel):
    """一条修订记录（五要素 + 溯源字段）。"""

    model_config = ConfigDict(extra="ignore")

    case_id: str = ""
    recorded_at: str = ""                                   # 记录时间
    iteration_type: str = "merge"                           # 类型
    type_label: str = ""                                    # 类型中文标签
    instruction: str = ""                                   # 用户说明摘要
    files: list[str] = Field(default_factory=list)          # 交付文件
    summary: str = ""                                       # 摘要摘录（留档小节正文）
    run_group: str | None = None
    artifact_id: str | None = None
    intent: str = ""                                        # 意图路由结果（merge/correct/…）

    def label(self) -> str:
        return self.type_label or TYPE_LABEL.get(self.iteration_type, self.iteration_type)


def digest(text: str | None, limit: int) -> str:
    """折叠空白后截断（超长加省略号）。"""
    body = " ".join(str(text or "").split())
    return body if len(body) <= limit else body[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------


def _insert_record_sync(record: RevisionRecord) -> int:
    """落一行 messages（seq 分配 + 插入同一事务内），返回 seq。"""

    def op(conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM messages WHERE case_id=?", (record.case_id,)
        ).fetchone()
        seq = int(row[0]) + 1
        conn.execute(
            """
            INSERT INTO messages(id, case_id, seq, role, step_key, content, meta_json, created_at)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                str(ULID()),
                record.case_id,
                seq,
                "system",
                RECORD_STEP_KEY,
                record.model_dump_json(),
                json.dumps({"event": RECORD_EVENT}, ensure_ascii=False),
                record.recorded_at or db.now_str(),
            ),
        )
        return seq

    return db.transaction(op)


async def record(
    case_id: str,
    *,
    iteration_type: str,
    instruction: str = "",
    files: Sequence[str] | None = None,
    summary: str = "",
    run_group: str | None = None,
    artifact_id: str | None = None,
    intent: str = "",
) -> RevisionRecord:
    """登记一条修订记录（五要素齐全；文本自动摘要截断）。"""
    item = RevisionRecord(
        case_id=case_id,
        recorded_at=db.now_str(),
        iteration_type=iteration_type,
        type_label=TYPE_LABEL.get(iteration_type, iteration_type),
        instruction=digest(instruction, INSTRUCTION_CHARS),
        files=[str(f) for f in (files or []) if str(f).strip()],
        summary=digest(summary, SUMMARY_CHARS),
        run_group=run_group,
        artifact_id=artifact_id,
        intent=intent,
    )
    await db.arun(_insert_record_sync, item)
    logger.info("修订记录已登记：case=%s type=%s files=%s", case_id, iteration_type, len(item.files))
    return item


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


def _explicit_records_sync(case_id: str) -> list[RevisionRecord]:
    rows = db.query_all(
        "SELECT content FROM messages WHERE case_id=? AND step_key=? ORDER BY seq ASC",
        (case_id, RECORD_STEP_KEY),
    )
    out: list[RevisionRecord] = []
    for row in rows:
        try:
            out.append(RevisionRecord.model_validate_json(row["content"]))
        except Exception as exc:  # noqa: BLE001 —— 单条坏记录不拖垮列表
            logger.warning("修订记录解析失败（已跳过）：%s", exc)
    return out


def _artifact_filenames_sync(case_id: str, run_group: str | None) -> list[str]:
    """同一 run_group 下的交底书正文类交付物文件名（按落盘顺序）。"""
    if not run_group:
        return []
    placeholders = ",".join("?" for _ in DOC_KINDS)
    rows = db.query_all(
        f"SELECT filename FROM artifacts WHERE case_id=? AND run_group=? "
        f"AND kind IN ({placeholders}) ORDER BY created_at ASC, version ASC",
        (case_id, run_group, *DOC_KINDS),
    )
    return [str(r["filename"]) for r in rows]


def _initial_record_sync(case_id: str) -> RevisionRecord | None:
    """初稿记录：由首版 disclosure_md（iteration_type=initial）合成。"""
    row = db.query_one(
        "SELECT * FROM artifacts WHERE case_id=? AND kind='disclosure_md' "
        "AND iteration_type='initial' ORDER BY version ASC LIMIT 1",
        (case_id,),
    )
    if row is None:
        return None
    files = _artifact_filenames_sync(case_id, row["run_group"]) or [str(row["filename"])]
    return RevisionRecord(
        case_id=case_id,
        recorded_at=str(row["created_at"]),
        iteration_type="initial",
        type_label=TYPE_LABEL["initial"],
        instruction="初次成文（边界录入 → 材料消化 → 专利点挖掘 → 查新 → 摘要预览 → 分章成文 → 自检 → 交付）",
        files=files,
        summary=digest(row["summary"] or "交底书定稿", SUMMARY_CHARS),
        run_group=row["run_group"],
        artifact_id=str(row["id"]),
    )


def list_records_sync(case_id: str) -> list[RevisionRecord]:
    """全部修订记录（初稿合成条 + 各轮迭代记录，按记录时间升序）。"""
    records = _explicit_records_sync(case_id)
    initial = _initial_record_sync(case_id)
    if initial is not None and not any(r.iteration_type == "initial" for r in records):
        records = [initial, *records]
    return records


async def list_records(case_id: str) -> list[RevisionRecord]:
    """`list_records_sync` 的异步包装。"""
    return await db.arun(list_records_sync, case_id)


# ---------------------------------------------------------------------------
# 导出「交底书修订对话记录.md」
# ---------------------------------------------------------------------------


def render_markdown(case_title: str, records: Sequence[RevisionRecord | Mapping[str, Any]]) -> str:
    """把累计记录渲染为「交底书修订对话记录.md」正文。"""
    items = [r if isinstance(r, RevisionRecord) else RevisionRecord.model_validate(dict(r)) for r in records]
    lines: list[str] = [
        f"# {LOG_TITLE}",
        "",
        f"**案件名称**：{(case_title or '未命名案件').strip()}",
        "",
        f"**记录导出时间**：{db.now_str()}",
        "",
        f"**累计轮次**：{len(items)}",
        "",
        "---",
        "",
    ]
    if not items:
        lines.append("（本案尚无交付记录。）")
        return "\n".join(lines).rstrip() + "\n"

    for index, item in enumerate(items, 1):
        lines.append(f"## 第 {index} 轮 · {item.label()}")
        lines.append("")
        lines.append(f"- **记录时间**：{item.recorded_at or '（未记录）'}")
        lines.append(f"- **类型**：{item.label()}" + (f"（意图：{item.intent}）" if item.intent else ""))
        lines.append(f"- **用户说明摘要**：{item.instruction or '（无）'}")
        if item.files:
            lines.append("- **交付文件**：")
            lines.extend(f"  - {name}" for name in item.files)
        else:
            lines.append("- **交付文件**：（本轮未产出新文件）")
        lines.append("- **摘要摘录**：")
        # 摘录以引用块呈现；剥掉行首的 markdown 标题记号，避免留档小节标题在记录里再成一级标题
        segments = [seg.lstrip("#").strip() for seg in (item.summary or "").splitlines()]
        segments = [seg for seg in segments if seg] or ["（无）"]
        lines.extend(f"  > {seg}" for seg in segments)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


async def export_log(
    case_id: str,
    *,
    case_title: str = "",
    run_group: str | None = None,
    records: Sequence[RevisionRecord] | None = None,
) -> ArtifactOut:
    """渲染并落 `revision_log_md` 交付物（每轮重写 = 新版本，内容为累计追加）。"""
    items = list(records) if records is not None else await list_records(case_id)
    text = render_markdown(case_title, items)
    return await artifacts_service.save_artifact(
        case_id,
        "revision_log_md",
        text,
        "md",
        title=LOG_TITLE,
        md_text="",  # 不从正文抽「案件名称」行，文件名固定用 LOG_TITLE
        run_group=run_group,
        iteration_type="export",
        summary=f"修订对话记录（累计 {len(items)} 轮）",
    )
