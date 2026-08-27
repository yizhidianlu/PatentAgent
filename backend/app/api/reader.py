"""专利解读专属 API（backend-architecture.md §3 / prompt-porting-spec.md §2 模块 C）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/cases/{id}/reader/report` | 结构化解读报告 `report.json`（meta + 11 节 blocks），供前端渲染 |
| GET | `/cases/{id}/reader/claim-tree` | 已确认的权要树 + 每权白话增量 + 树 lint 结果 |

两个端点都是只读的：报告与权要树由流水线（`pipelines/reader.py`）写进
`cases.state_json`，交付的 markdown 另有版本化的 `reader_note_md` 交付物；
本模块只负责取用与拼装，绝不重算。报告尚未生成时返回 404（前端据此显示空态）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from ..db import database as db
from ..models.reader import (
    ClaimDeltas,
    ClaimTree,
    ReaderClaimTreeOut,
    ReaderReport,
    ReaderReportOut,
)
from .deps import client_ip, current_user, resolve_case_sync

logger = logging.getLogger(__name__)

router = APIRouter(tags=["专利解读"])


def _get_case_row(
    case_id: str, user: dict[str, Any], *, ip: str | None = None
) -> dict[str, Any]:
    """取案件（先做属主隔离，再判模块）。"""
    row = resolve_case_sync(case_id, user, ip=ip)
    if row["module"] != "reader":
        raise HTTPException(status_code=400, detail="该案件不是专利解读案件")
    return row


def _case_state(row) -> dict[str, Any]:
    try:
        return json.loads(row["state_json"] or "{}")
    except json.JSONDecodeError:  # pragma: no cover —— state_json 由平台写入，理论上恒合法
        return {}


def _latest_note_artifact(case_id: str) -> sqlite3.Row | None:
    return db.query_one(
        "SELECT * FROM artifacts WHERE case_id=? AND kind='reader_note_md' "
        "ORDER BY version DESC LIMIT 1",
        (case_id,),
    )


def _load_report_sync(
    case_id: str, user: dict[str, Any], *, ip: str | None = None
) -> dict[str, Any]:
    """报告快照：state 里的 report.json + 最近一版 reader_note_md 交付物。"""
    row = _get_case_row(case_id, user, ip=ip)
    state = _case_state(row)
    report = state.get("report")
    artifact = _latest_note_artifact(case_id)

    markdown = state.get("report_markdown")
    if not markdown and artifact is not None:
        path = Path(artifact["stored_path"])
        if path.is_file():
            markdown = path.read_text(encoding="utf-8", errors="replace")

    if not isinstance(report, dict) or not report.get("sections"):
        raise HTTPException(
            status_code=404, detail="该案件尚未生成解读报告（请先跑完「校对与交付」步骤）"
        )
    return {
        "report": report,
        "markdown": markdown,
        "artifact": dict(artifact) if artifact is not None else None,
        "lint": state.get("report_lint") or [],
        "unresolved": (state.get("self_check") or {}).get("unresolved") or [],
    }


def _load_claim_tree_sync(
    case_id: str, user: dict[str, Any], *, ip: str | None = None
) -> dict[str, Any]:
    row = _get_case_row(case_id, user, ip=ip)
    state = _case_state(row)
    tree = state.get("claim_tree")
    if not isinstance(tree, dict) or not tree.get("claims"):
        raise HTTPException(
            status_code=404, detail="该案件尚未产出权利要求树（请先跑完「权利要求树」步骤）"
        )
    return {
        "tree": tree,
        "deltas": state.get("claim_deltas") or {},
        "lint": state.get("claim_tree_lint") or [],
        "resolved": bool(state.get("claim_tree_resolved")),
    }


@router.get(
    "/cases/{case_id}/reader/report",
    response_model=ReaderReportOut,
    summary="结构化解读报告（report.json：meta + 11 节 blocks）",
)
async def get_report(
    case_id: str,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> ReaderReportOut:
    data = await db.arun(_load_report_sync, case_id, user, ip=client_ip(request))
    artifact = data["artifact"]
    return ReaderReportOut(
        report=ReaderReport.model_validate(data["report"]),
        markdown=data["markdown"],
        artifact_id=artifact["id"] if artifact else None,
        version=artifact["version"] if artifact else None,
        filename=artifact["filename"] if artifact else None,
        lint=data["lint"],
        unresolved=[str(x) for x in data["unresolved"]],
    )


@router.get(
    "/cases/{case_id}/reader/claim-tree",
    response_model=ReaderClaimTreeOut,
    summary="已确认的权利要求树 + 每权白话增量 + 树 lint",
)
async def get_claim_tree(
    case_id: str,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> ReaderClaimTreeOut:
    data = await db.arun(_load_claim_tree_sync, case_id, user, ip=client_ip(request))
    tree = ClaimTree.model_validate(data["tree"])
    deltas = ClaimDeltas.model_validate(data["deltas"] or {})
    return ReaderClaimTreeOut(
        tree=tree,
        deltas=deltas.deltas,
        summary=tree.summary_line(),
        lint=data["lint"],
        ambiguities_resolved=data["resolved"],
    )
