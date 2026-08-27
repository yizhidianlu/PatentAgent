"""流水线 API（四模块共用引擎；backend-architecture.md §3.4 契约）。

- POST /cases/{id}/pipeline/start  body=模块初始载荷 → 202 {"run_group":"initial"}（409 if running）
- GET  /cases/{id}/pipeline/state → {run_group, steps[], pending_interaction|null}
- POST /cases/{id}/pipeline/input  {step_key, payload} → 202（非 waiting_user / step 不匹配 → 409）
- POST /cases/{id}/pipeline/resume|cancel → 202；POST /pipeline/retry {step_key?} → 202
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from ..db import database as db
from ..models.common import Ok
from ..models.pipeline import (
    PendingInteractionOut,
    PipelineInputBody,
    PipelineRetryBody,
    PipelineStartOut,
    PipelineStateOut,
    StepStateOut,
)
from ..services import progress as progress_service
from ..pipelines import engine, registry
from .deps import client_ip, current_user, enforce_quota, resolve_case_sync

router = APIRouter(tags=["流水线"])


def _build_steps_or_400(row):
    try:
        return registry.build_steps(dict(row))
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _latest_runs(case_id: str, run_group: str) -> dict[str, sqlite3.Row]:
    """每个 step_key 取最大 attempt 的一行。"""
    rows = db.query_all(
        "SELECT * FROM pipeline_runs WHERE case_id=? AND run_group=? ORDER BY attempt ASC",
        (case_id, run_group),
    )
    latest: dict[str, sqlite3.Row] = {}
    for r in rows:  # attempt 升序遍历 → 字典覆盖后留下的即各步骤最大 attempt
        latest[r["step_key"]] = r
    return latest


@router.post(
    "/cases/{case_id}/pipeline/start",
    status_code=202,
    response_model=PipelineStartOut,
    summary="启动流水线（body=模块初始载荷；运行中 409）",
)
async def start_pipeline(
    case_id: str,
    request: Request,
    body: dict[str, Any] | None = Body(default=None),
    user: dict[str, Any] = Depends(current_user),
) -> PipelineStartOut:
    row = await db.arun(
        resolve_case_sync, case_id, user, ip=client_ip(request), write=True
    )
    enforce_quota(user)
    if engine.is_running(case_id):
        raise HTTPException(status_code=409, detail="该案件的流水线正在运行中")
    steps = _build_steps_or_400(row)
    try:
        engine.start(case_id, steps, run_group="initial", start_payload=body or {})
    except engine.PipelineConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return PipelineStartOut(run_group="initial")


@router.get(
    "/cases/{case_id}/pipeline/state",
    response_model=PipelineStateOut,
    summary="流水线状态：run_group + 各步骤最新状态 + 挂起的人机交互",
)
async def pipeline_state(
    case_id: str,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> PipelineStateOut:
    row = await db.arun(resolve_case_sync, case_id, user, ip=client_ip(request))
    try:
        state = json.loads(row["state_json"] or "{}")
    except json.JSONDecodeError:
        state = {}
    run_group = state.get("_run_group") or "initial"

    step_defs: list[dict[str, Any]] = state.get("_steps") or []
    if not step_defs:  # 尚未 start 过：尽力从注册表列出全 pending 的步骤表
        try:
            step_defs = [
                {"key": s.key, "name_zh": s.name_zh} for s in registry.build_steps(dict(row))
            ]
        except KeyError:
            step_defs = []

    latest = await db.arun(_latest_runs, case_id, run_group)
    steps = []
    for d in step_defs:
        run = latest.get(d["key"])
        steps.append(
            StepStateOut(
                key=d["key"],
                name_zh=d.get("name_zh") or d["key"],
                status=run["status"] if run is not None else "pending",
                attempt=int(run["attempt"]) if run is not None else 0,
                error=run["error"] if run is not None else None,
            )
        )

    pending = engine.get_pending(case_id)
    return PipelineStateOut(
        run_group=run_group,
        steps=steps,
        pending_interaction=PendingInteractionOut(**pending) if pending else None,
        progress=progress_service.snapshot(case_id),
    )


@router.post(
    "/cases/{case_id}/pipeline/input",
    status_code=202,
    response_model=Ok,
    summary="回答门控（非 waiting_user 或 step_key 不匹配 → 409）",
)
async def pipeline_input(
    case_id: str,
    body: PipelineInputBody,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> Ok:
    await db.arun(resolve_case_sync, case_id, user, ip=client_ip(request), write=True)
    try:
        engine.submit_input(case_id, body.step_key, body.payload)
    except engine.PipelineConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Ok()


@router.post(
    "/cases/{case_id}/pipeline/resume",
    status_code=202,
    response_model=PipelineStartOut,
    summary="服务重启后续跑：从第一个非 done 步骤重建（waiting_user 步骤会重发 interaction_required）",
)
async def pipeline_resume(
    case_id: str,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> PipelineStartOut:
    row = await db.arun(
        resolve_case_sync, case_id, user, ip=client_ip(request), write=True
    )
    enforce_quota(user)
    if engine.is_running(case_id):
        raise HTTPException(status_code=409, detail="该案件的流水线正在运行中")
    steps = _build_steps_or_400(row)
    run_group = engine.resolve_run_group(row)
    try:
        engine.start(case_id, steps, run_group=run_group)
    except engine.PipelineConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return PipelineStartOut(run_group=run_group)


@router.post(
    "/cases/{case_id}/pipeline/retry",
    status_code=202,
    response_model=PipelineStartOut,
    summary="失败步重试（attempt+1 新 pipeline_runs 行）并继续后续步骤",
)
async def pipeline_retry(
    case_id: str,
    request: Request,
    body: PipelineRetryBody | None = Body(default=None),
    user: dict[str, Any] = Depends(current_user),
) -> PipelineStartOut:
    row = await db.arun(
        resolve_case_sync, case_id, user, ip=client_ip(request), write=True
    )
    enforce_quota(user)
    if engine.is_running(case_id):
        raise HTTPException(status_code=409, detail="该案件的流水线正在运行中")
    run_group = engine.resolve_run_group(row)
    latest = await db.arun(_latest_runs, case_id, run_group)
    failed_steps = [k for k, r in latest.items() if r["status"] == "failed"]
    if body is not None and body.step_key:
        run = latest.get(body.step_key)
        if run is None or run["status"] != "failed":
            raise HTTPException(status_code=409, detail=f"步骤 {body.step_key} 不处于 failed 状态")
    elif not failed_steps:
        raise HTTPException(status_code=409, detail="当前没有失败的步骤可重试")
    steps = _build_steps_or_400(row)
    try:
        engine.start(case_id, steps, run_group=run_group)
    except engine.PipelineConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return PipelineStartOut(run_group=run_group)


@router.post(
    "/cases/{case_id}/pipeline/cancel",
    status_code=202,
    response_model=Ok,
    summary="取消当前流水线任务（run 行回写 cancelled，案件回 draft）",
)
async def pipeline_cancel(
    case_id: str,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> Ok:
    await db.arun(resolve_case_sync, case_id, user, ip=client_ip(request), write=True)
    try:
        engine.cancel(case_id)
    except engine.PipelineConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Ok()
