"""交底书专属 API（backend-architecture.md §3.5）。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/cases/{id}/disclosure/iterate` | `{mode:'merge'|'correct'|'auto', instruction, file_ids[]}` → 202，启动迭代流水线（run_group 递增；运行中 409） |
| GET | `/cases/{id}/disclosure/revisions` | 修订记录列表（五要素）+ 最近一版「交底书修订对话记录.md」 |

迭代流水线本体在 `pipelines/disclosure_iterate.py`；本模块只做参数校验、并发保护与
run_group 分配，随后交给共用的流水线引擎（推进过程走 `/pipeline/state|input` 与 SSE）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..db import database as db
from ..pipelines import disclosure as main_disclosure
from ..pipelines import disclosure_iterate, engine, registry
from ..pipelines.disclosure import PatentTypeNotSupportedError
from ..services import revision_log
from .deps import client_ip, current_user, enforce_quota, resolve_case_sync

logger = logging.getLogger(__name__)

router = APIRouter(tags=["交底书"])


# ---------------------------------------------------------------------------
# 迭代轮的 resume / retry 兜底
#
# `/pipeline/resume|retry` 会用 `engine.resolve_run_group()` 读出案件最后一次的
# run_group，再经 `registry.build_steps()` 重建步骤表。若最后一次是**未跑完的迭代轮**
# （进程重启 / 某步 failed），注册表原本只会给出主流水线八步 —— 那会在 iteration-N 这个
# run_group 下把八步全当 pending 重跑一遍，覆盖已交付的工作内存。
#
# 故在此把 'disclosure' 的步骤工厂包一层：**仅当**案件最后一次 run_group 形如
# `iteration-<n>` 且该轮尚未全部 done 时给出迭代五步，其余一律委托给主流水线工厂
# （M5 扩展实用新型 / 外观分支时无需改动本处）。
# ---------------------------------------------------------------------------


def _iteration_unfinished_sync(case_id: str, run_group: str) -> bool:
    """该迭代轮是否尚未跑完（按每个 step_key 的最大 attempt 判定）。"""
    rows = db.query_all(
        "SELECT step_key, status FROM pipeline_runs WHERE case_id=? AND run_group=? "
        "ORDER BY attempt ASC",
        (case_id, run_group),
    )
    if not rows:
        return False
    latest: dict[str, str] = {}
    for row in rows:  # attempt 升序 → 覆盖后留下每步最大 attempt 的状态
        latest[str(row["step_key"])] = str(row["status"])
    if len(latest) < len(disclosure_iterate.ITERATION_STEP_KEYS):
        return True
    return any(status != "done" for status in latest.values())


def build_disclosure_steps(case: dict[str, Any]) -> list[Any]:
    """交底书模块的步骤工厂（主流水线八步 / 未跑完的迭代轮五步）。"""
    case_id = str(case.get("id") or "")
    run_group = engine.resolve_run_group(case)
    if (
        case_id
        and str(run_group).startswith("iteration-")
        and _iteration_unfinished_sync(case_id, run_group)
    ):
        logger.info("案件 %s 的 %s 尚未跑完，按迭代步骤表重建", case_id, run_group)
        return disclosure_iterate.build_iteration_steps(case)
    return main_disclosure.build_steps(case)


registry.register("disclosure", build_disclosure_steps)


# ---------------------------------------------------------------------------
# 契约模型
# ---------------------------------------------------------------------------


class DisclosureIterateIn(BaseModel):
    """迭代请求体。`auto` 时由 IT0 意图路由判类（merge/correct/question/new_case）。"""

    mode: Literal["merge", "correct", "auto"] = "auto"
    instruction: str = Field(default="", description="本轮用户说明（补材料说明 / 纠正点 / 提问）")
    file_ids: list[str] = Field(default_factory=list, description="本轮新材料的文件 id（可空）")


class DisclosureIterateOut(BaseModel):
    """迭代启动结果。"""

    run_group: str
    mode: str
    round: int


class RevisionRecordOut(BaseModel):
    """一条修订记录（五要素：记录时间 / 类型 / 用户说明摘要 / 交付文件 / 摘要摘录）。"""

    recorded_at: str
    iteration_type: str
    type_label: str
    instruction: str
    files: list[str]
    summary: str
    run_group: str | None = None
    artifact_id: str | None = None
    intent: str = ""


class RevisionListOut(BaseModel):
    """修订记录列表 + 最近一版修订对话记录文件。"""

    case_id: str
    count: int
    records: list[RevisionRecordOut]
    log_artifact_id: str | None = None
    log_filename: str | None = None


# ---------------------------------------------------------------------------
# 公共
# ---------------------------------------------------------------------------


def _get_case_row(
    case_id: str,
    user: dict[str, Any],
    *,
    ip: str | None = None,
    write: bool = False,
) -> dict[str, Any]:
    """取案件（先做属主隔离，再判模块）。"""
    row = resolve_case_sync(case_id, user, ip=ip, write=write)
    if row["module"] != "disclosure":
        raise HTTPException(status_code=400, detail="该案件不是交底书案件")
    return row


def _case_state(row) -> dict[str, Any]:
    try:
        return json.loads(row["state_json"] or "{}")
    except json.JSONDecodeError:
        return {}


def _latest_log_sync(case_id: str) -> sqlite3.Row | None:
    return db.query_one(
        "SELECT * FROM artifacts WHERE case_id=? AND kind='revision_log_md' "
        "ORDER BY version DESC LIMIT 1",
        (case_id,),
    )


def _has_delivered_sync(case_id: str) -> bool:
    return (
        db.query_one(
            "SELECT id FROM artifacts WHERE case_id=? AND kind='disclosure_md' LIMIT 1", (case_id,)
        )
        is not None
    )


def _require_own_files_sync(case_id: str, file_ids: list[str]) -> None:
    """校验 file_ids 全部存在**且属于本案件**。

    不校验的话，`pipelines/disclosure._material_rows_sync` 过滤不到任何行时会**静默
    回落到本案件的全部上传件**——用户以为只并入了新材料，实际把全部旧材料又当新材料
    重跑了一遍（既是越权面，也白烧一轮 LLM 配额）。故在入口就 404 掉。
    """
    if not file_ids:
        return
    rows = db.query_all(
        f"SELECT id, case_id FROM files WHERE id IN ({','.join('?' * len(file_ids))})",
        tuple(file_ids),
    )
    owned = {str(r["id"]) for r in rows if str(r["case_id"]) == case_id}
    missing = [f for f in file_ids if f not in owned]
    if missing:
        raise HTTPException(
            status_code=404,
            detail=f"文件不存在或不属于本案件：{'、'.join(missing)}",
        )


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


@router.post(
    "/cases/{case_id}/disclosure/iterate",
    status_code=202,
    response_model=DisclosureIterateOut,
    summary="在已交付定稿上启动一轮迭代（合并 / 纠正 / 自动判类）",
)
async def iterate(
    case_id: str,
    body: DisclosureIterateIn,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> DisclosureIterateOut:
    row = await db.arun(
        _get_case_row, case_id, user, ip=client_ip(request), write=True
    )
    enforce_quota(user)
    if engine.is_running(case_id):
        raise HTTPException(status_code=409, detail="该案件的流水线正在运行中，请先取消或等待完成")

    file_ids = [str(f) for f in body.file_ids if str(f).strip()]
    await db.arun(_require_own_files_sync, case_id, file_ids)

    state = _case_state(row)
    delivered = bool(str(state.get("final_markdown") or "").strip()) or await db.arun(
        _has_delivered_sync, case_id
    )
    if not delivered:
        raise HTTPException(
            status_code=409, detail="该案件尚未产出交底书定稿，无法迭代（请先跑完主流水线并交付）"
        )

    try:
        steps = disclosure_iterate.build_iteration_steps(dict(row))
    except PatentTypeNotSupportedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    run_group = await disclosure_iterate.next_run_group(case_id)
    payload = {
        "mode": body.mode,
        "instruction": body.instruction.strip(),
        "file_ids": file_ids,
    }
    try:
        engine.start(case_id, steps, run_group=run_group, start_payload=payload)
    except engine.PipelineConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    logger.info("交底书迭代已启动：case=%s run_group=%s mode=%s", case_id, run_group, body.mode)
    return DisclosureIterateOut(
        run_group=run_group,
        mode=body.mode,
        round=disclosure_iterate.iteration_round(run_group),
    )


@router.get(
    "/cases/{case_id}/disclosure/revisions",
    response_model=RevisionListOut,
    summary="修订记录列表（五要素）+ 最近一版「交底书修订对话记录.md」",
)
async def revisions(
    case_id: str,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> RevisionListOut:
    await db.arun(_get_case_row, case_id, user, ip=client_ip(request))
    records = await revision_log.list_records(case_id)
    log_row = await db.arun(_latest_log_sync, case_id)
    return RevisionListOut(
        case_id=case_id,
        count=len(records),
        records=[
            RevisionRecordOut(
                recorded_at=r.recorded_at,
                iteration_type=r.iteration_type,
                type_label=r.label(),
                instruction=r.instruction,
                files=list(r.files),
                summary=r.summary,
                run_group=r.run_group,
                artifact_id=r.artifact_id,
                intent=r.intent,
            )
            for r in records
        ],
        log_artifact_id=log_row["id"] if log_row is not None else None,
        log_filename=log_row["filename"] if log_row is not None else None,
    )
