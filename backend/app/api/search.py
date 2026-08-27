"""查新 API（backend-architecture.md §3.5 交底书专属）。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/cases/{id}/search/cnipa` | 触发国知局检索（后台任务，进度经 SSE `search_progress`） |
| GET | `/cases/{id}/search/hits` | 命中列表 + 会话历史 |
| POST | `/cases/{id}/search/hits` | 人工兜底录入（`manual_entry=1`） |
| PATCH | `/search/hits/{id}` | 勾选 / 回写消化摘要 |
| POST | `/cases/{id}/search/skip` | 明确跳过查新（1.1 须如实写明未检索） |
| GET | `/search/browser-probe` | 浏览器可用性探测（Chrome → Edge → Chromium） |

检索失败不返回 5xx：`services/cnipa.search()` 已把超时 / WAF / 零解析统一降级为
`status='failed'` 写库，前端据 `GET .../search/hits` 的 `latest_status` 呈现三选项
（重试 / 粘贴在先文献 / 跳过）。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..db import database as db
from ..models.search import (
    BrowserProbe,
    ManualHitsIn,
    SearchHit,
    SearchHitPatchIn,
    SearchHitsOut,
    SearchSkipIn,
    SearchStartIn,
    SearchStartOut,
)
from ..services import cnipa
from .deps import client_ip, current_user, resolve_case_sync

logger = logging.getLogger(__name__)

router = APIRouter(tags=["查新"])


def _resolve_hit_sync(
    hit_id: str, user: dict[str, Any], *, ip: str | None = None
) -> dict[str, Any]:
    """取命中行并经其所属案件做属主校验；非本人（非管理员）一律 404。"""
    row = db.query_one("SELECT * FROM search_hits WHERE id=?", (hit_id,))
    if row is None:
        raise HTTPException(status_code=404, detail=f"命中不存在：{hit_id}")
    data = dict(row)
    try:
        resolve_case_sync(str(data.get("case_id") or ""), user, ip=ip, write=True)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=404, detail=f"命中不存在：{hit_id}") from exc
        raise
    return data


@router.post(
    "/cases/{case_id}/search/cnipa",
    status_code=202,
    response_model=SearchStartOut,
    summary="触发国知局查新（后台任务；进度经 SSE search_progress 推送）",
)
async def start_cnipa_search(
    case_id: str,
    body: SearchStartIn,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> SearchStartOut:
    row = await db.arun(
        resolve_case_sync, case_id, user, ip=client_ip(request), write=True
    )
    if cnipa.is_searching(case_id):
        raise HTTPException(status_code=409, detail="该案件的查新任务正在运行中")

    terms = cnipa.normalize_terms(body.terms)
    if not terms:
        raise HTTPException(status_code=422, detail="检索词为空")
    patent_type = cnipa.normalize_type(body.patent_type or row["patent_type"])

    try:
        cnipa.start_background_search(
            case_id, terms, patent_type, use_cache=body.use_cache
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return SearchStartOut(status="running", terms=terms, patent_type=patent_type)


@router.get(
    "/cases/{case_id}/search/hits",
    response_model=SearchHitsOut,
    summary="命中列表 + 检索会话历史（含最近一次状态/失败原因）",
)
async def get_hits(
    case_id: str,
    request: Request,
    selected_only: bool = Query(default=False, description="只返回已勾选的命中"),
    user: dict[str, Any] = Depends(current_user),
) -> SearchHitsOut:
    await db.arun(resolve_case_sync, case_id, user, ip=client_ip(request))
    hits = await cnipa.list_hits(case_id, selected_only=selected_only)
    queries = await cnipa.list_queries(case_id)
    latest = queries[0] if queries else None
    return SearchHitsOut(
        hits=hits,
        queries=queries,
        count=len(hits),
        selected_count=sum(1 for h in hits if h.selected),
        latest_status=latest.status if latest else None,
        latest_error=latest.error if latest else None,
    )


@router.post(
    "/cases/{case_id}/search/hits",
    status_code=201,
    response_model=list[SearchHit],
    summary="人工兜底录入在先文献（URL 必填，manual_entry=1）",
)
async def add_hits(
    case_id: str,
    body: ManualHitsIn,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> list[SearchHit]:
    await db.arun(resolve_case_sync, case_id, user, ip=client_ip(request), write=True)
    try:
        return await cnipa.add_manual_hits(case_id, body.hits, note=body.note)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.patch(
    "/search/hits/{hit_id}",
    response_model=SearchHit,
    summary="勾选/取消勾选，或回写消化摘要",
)
async def patch_hit(
    hit_id: str,
    body: SearchHitPatchIn,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> SearchHit:
    if body.selected is None and body.digest is None:
        raise HTTPException(status_code=422, detail="请至少提供 selected 或 digest")
    await db.arun(_resolve_hit_sync, hit_id, user, ip=client_ip(request))
    try:
        hit: SearchHit | None = None
        if body.selected is not None:
            hit = await cnipa.set_selected(hit_id, body.selected)
        if body.digest is not None:
            hit = await cnipa.set_digest(hit_id, body.digest)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    assert hit is not None
    return hit


@router.post(
    "/cases/{case_id}/search/skip",
    status_code=202,
    response_model=SearchStartOut,
    summary="明确跳过查新（1.1 将如实写明未检索，禁止编造检索结果）",
)
async def skip_search(
    case_id: str,
    request: Request,
    body: SearchSkipIn | None = None,
    user: dict[str, Any] = Depends(current_user),
) -> SearchStartOut:
    await db.arun(resolve_case_sync, case_id, user, ip=client_ip(request), write=True)
    query = await cnipa.skip_search(case_id, (body.reason if body else "") or "")
    return SearchStartOut(query_id=query.id, status="done", terms=[], patent_type="all")


@router.get(
    "/search/browser-probe",
    response_model=BrowserProbe,
    summary="浏览器可用性探测（本机 Chrome → Edge → 自带 Chromium）",
)
async def browser_probe() -> BrowserProbe:
    return await cnipa.probe_browser()
