"""OA 案例库 API（backend-architecture.md §3.7 + guardrails.md 暂停点 P4）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/oa/library?tag=&defect_type=&status=&q=&limit=&offset=` | 案例列表（分页 + 过滤） |
| POST | `/oa/library/ingest` | 材料 → (llm:STRUCT) 案例笔记草稿（multipart 上传件 或 `{file_id}` 复用已上传件） |
| GET | `/oa/library/search?q=&k=&defect_type=&statute=&patent_type=&tag=` | 案例检索（**响应必带 `retrieval_mode`**） |
| GET | `/oa/library/stats` | 索引状态摘要（案例数/块数/向量数/维度/扩展可用性） |
| POST | `/oa/library/rebuild?wait=` | 全量重建向量库（默认后台任务 + 轮询；`wait=true` 同步执行） |
| GET | `/oa/library/rebuild/status` | 重建任务状态（轮询用） |
| GET/PUT | `/oa/library/{id}` | 人审详情 / 更新；PUT `{status:'confirmed'}` → 切块 + 嵌入 |
| DELETE | `/oa/library/{id}` | 删案例（连带 chunks + 向量） |

纪律：
- **只有 `status='confirmed'` 的案例进检索库**：PUT 到 confirmed 时由
  `services/oa_library.update()` 同步触发切块 + 向量化；回落 draft 时清空检索块；
- **检索方式必须明示**：`GET /oa/library/search` 的响应 `retrieval_mode` ∈
  vector / brute / keyword（三级降级链见 `services/vector`），未配 embedding 或
  sqlite-vec 扩展缺失时接口照常 200，只是模式降级 + `message` 说明原因；
- **嵌入失败不阻断入库**：`services/oa_library.reindex()` 把错误记在
  `_embed_error`（详情接口的 `embed_error` 字段），案例仍以关键词模式可检索。

本模块只做参数校验、请求体解包与错误码映射，业务逻辑全在
`services/oa_library.py` 与 `services/vector.py`。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from starlette.datastructures import UploadFile as StarletteUploadFile

from ..db import database as db
from ..models.common import Ok, Page
from ..models.oa import (
    LIBRARY_STATUSES,
    OaLibraryDetail,
    OaLibraryIngestOut,
    OaLibraryItem,
    OaLibraryUpdateIn,
    OaRebuildOut,
    OaSearchOut,
)
from ..services import llm as llm_service
from ..services import oa_library, vector
from .deps import client_ip, current_user, require_admin, resolve_file_sync, viewer_of

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/oa", tags=["OA 案例库"])

#: 列表分页上限
MAX_LIMIT = 200
#: 检索返回条数上限
MAX_K = 50


# ---------------------------------------------------------------------------
# 本模块专属契约（跨模块复用的契约在 models/oa.py）
# ---------------------------------------------------------------------------


class OaLibraryUpdateOut(BaseModel):
    """PUT /oa/library/{id} 响应：更新后的详情 + 服务端归一化说明。"""

    item: OaLibraryDetail
    normalized: list[str] = Field(default_factory=list)
    reindexed: bool = False               # 本次是否触发了切块/向量化


class OaRebuildStatusOut(BaseModel):
    """GET /oa/library/rebuild/status 响应（后台重建任务的轮询契约）。"""

    running: bool = False
    started_at: str = ""
    finished_at: str = ""
    error: str = ""
    result: dict[str, Any] | None = None  # 完成后为 services/vector.rebuild() 的返回


class OaLibraryStatsOut(BaseModel):
    """GET /oa/library/stats 响应：索引状态摘要。"""

    cases: int = 0
    confirmed: int = 0
    chunks: int = 0
    vectors: int = 0
    vec_available: bool = False
    vec_table_dim: int | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    embedding_configured: bool = False
    retrieval_mode: str = "keyword"       # 当前配置下检索会走的模式
    message: str = ""


# ---------------------------------------------------------------------------
# 公共
# ---------------------------------------------------------------------------


def current_mode() -> str:
    """当前配置下检索会走的模式（vector / brute / keyword）。"""
    if not vector.is_configured():
        return "keyword"
    return "vector" if vector.vec_available() else "brute"


def _read_file_material_sync(
    file_id: str, user: dict[str, Any], *, ip: str | None = None
) -> dict[str, Any]:
    """已上传件（files 表）→ 案例材料 `{name, text, path, error}`（含属主校验）。"""
    try:
        row = resolve_file_sync(file_id, user, ip=ip)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=404, detail=f"文件不存在：{file_id}") from exc
        raise
    md_path = row["md_path"]
    if not md_path:
        raise HTTPException(
            status_code=422,
            detail=f"文件《{row['orig_name']}》没有可解析文本（未转换或为二进制文件）",
        )
    path = Path(str(md_path))
    if not path.is_file():
        raise HTTPException(status_code=422, detail=f"文件《{row['orig_name']}》的转换文本已不存在于磁盘")
    return {
        "name": str(row["orig_name"]),
        "text": path.read_text(encoding="utf-8", errors="replace"),
        "path": str(row["stored_path"]),
        "error": "",
    }


async def _collect_materials(request: Request, user: dict[str, Any]) -> dict[str, Any]:
    """解包 ingest 请求体：multipart（files[] + 表单字段）或 JSON（{file_id|file_ids}）。

    返回 `{materials, source_paths, notes, outcome, status, case_id, errors}`。
    """
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    materials: list[dict[str, Any]] = []
    source_paths: list[str] = []
    errors: list[str] = []
    file_ids: list[str] = []
    notes = outcome = case_id = ""
    status = "draft"

    if content_type == "application/json" or not content_type:
        try:
            body = await request.json()
        except Exception as exc:  # 请求体不是合法 JSON
            raise HTTPException(status_code=422, detail=f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=422, detail="请求体须为对象（{file_id} 或 {file_ids:[…]}）")
        raw_ids = body.get("file_ids") or body.get("file_id") or []
        if isinstance(raw_ids, str):
            raw_ids = [raw_ids]
        file_ids = [str(x).strip() for x in raw_ids if str(x).strip()]
        notes = str(body.get("notes") or "")
        outcome = str(body.get("outcome") or "")
        case_id = str(body.get("case_id") or "")
        status = str(body.get("status") or "draft")
    else:
        form = await request.form()
        uploads: list[StarletteUploadFile] = []
        for key in ("files", "file"):
            uploads.extend(v for v in form.getlist(key) if isinstance(v, StarletteUploadFile))
        for key in ("file_ids", "file_id"):
            for value in form.getlist(key):
                if isinstance(value, str):
                    file_ids.extend(x.strip() for x in value.split(",") if x.strip())
        notes = str(form.get("notes") or "")
        outcome = str(form.get("outcome") or "")
        case_id = str(form.get("case_id") or "")
        status = str(form.get("status") or "draft")

        for upload in uploads:
            payload = await upload.read()
            if not payload:
                errors.append(f"《{upload.filename or '未命名'}》为空文件")
                continue
            item = await db.arun(
                oa_library.read_material_sync, str(upload.filename or "case"), payload
            )
            if item.get("error"):
                errors.append(f"《{item['name']}》转换失败：{item['error']}")
            if str(item.get("text") or "").strip():
                materials.append({"name": item["name"], "text": item["text"], "role": "案例材料"})
            source_paths.append(str(item.get("path") or ""))

    for file_id in file_ids:
        item = await db.arun(
            _read_file_material_sync, file_id, user, ip=client_ip(request)
        )
        if str(item.get("text") or "").strip():
            materials.append({"name": item["name"], "text": item["text"], "role": "案例材料"})
        else:
            errors.append(f"《{item['name']}》转换文本为空")
        source_paths.append(str(item.get("path") or ""))

    if status not in LIBRARY_STATUSES:
        status = "draft"
    return {
        "materials": materials,
        "source_paths": [p for p in source_paths if p],
        "notes": notes,
        "outcome": outcome,
        "status": status,
        "case_id": case_id,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# 列表
# ---------------------------------------------------------------------------


@router.get(
    "/library",
    response_model=Page[OaLibraryItem],
    summary="案例库列表（tag / defect_type / status 过滤 + q 关键词模糊匹配 + 分页）",
)
async def list_library(
    tag: str | None = Query(default=None, description="标签精确匹配"),
    defect_type: str | None = Query(default=None, description="缺陷类型精确匹配"),
    status: str | None = Query(default=None, description="draft | confirmed"),
    q: str | None = Query(default=None, description="标题/领域/条款/标签/笔记正文的关键词模糊匹配"),
    limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    user: dict[str, Any] = Depends(current_user),
) -> Page[OaLibraryItem]:
    if status is not None and status not in LIBRARY_STATUSES:
        raise HTTPException(
            status_code=422, detail=f"status 只能是 {' / '.join(LIBRARY_STATUSES)}"
        )
    items, total = await oa_library.list_items(
        tag=tag, defect_type=defect_type, status=status, q=q, limit=limit, offset=offset,
        viewer=viewer_of(user),
    )
    return Page[OaLibraryItem](items=items, total=total, limit=limit, offset=offset)


# ---------------------------------------------------------------------------
# ingest（静态路径须先于 /library/{id} 声明）
# ---------------------------------------------------------------------------


@router.post(
    "/library/ingest",
    status_code=201,
    response_model=OaLibraryIngestOut,
    summary="导入案例材料 → 案例笔记草稿（multipart files[]，或 JSON {file_id|file_ids}）",
)
async def ingest_case(
    request: Request, user: dict[str, Any] = Depends(current_user)
) -> OaLibraryIngestOut:
    """材料 → (srv) 转 markdown → (llm:STRUCT) 脱敏案例笔记草稿 → `status='draft'` 落库。

    两种提交方式：
    - `multipart/form-data`：`files`（可多份 PDF/md/docx…）+ 可选表单字段
      `notes` / `outcome` / `status` / `case_id`；
    - `application/json`：`{"file_id": "…"}` 或 `{"file_ids": ["…"]}`（复用 files 表
      里已上传并转换过的文件）+ 同名可选字段。

    默认落 `draft`：**须经 `PUT {status:'confirmed'}` 人审确认后才切块入检索库**。
    """
    parsed = await _collect_materials(request, user)
    if not parsed["materials"]:
        detail = "没有可用于生成案例笔记的材料文本（未提交文件，或转换失败/内容为空）"
        if parsed["errors"]:
            detail += "：" + "；".join(parsed["errors"])
        raise HTTPException(status_code=422, detail=detail)

    try:
        result = await oa_library.ingest(
            parsed["materials"],
            notes=parsed["notes"],
            outcome=parsed["outcome"],
            source_paths=parsed["source_paths"],
            status=parsed["status"],
            case_id=parsed["case_id"] or None,
            user_id=str(user["id"]),
        )
    except llm_service.LlmNotConfiguredError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    out = OaLibraryIngestOut.model_validate(result)
    if parsed["errors"]:
        out.normalized = [*out.normalized, *parsed["errors"]]
    return out


# ---------------------------------------------------------------------------
# 检索（响应必带 retrieval_mode）
# ---------------------------------------------------------------------------


@router.get(
    "/library/search",
    response_model=OaSearchOut,
    summary="案例检索（三级降级链；响应必带 retrieval_mode：vector / brute / keyword）",
)
async def search_library(
    q: str = Query(default="", description="检索式（缺陷类型 + 条款 + 审查员观点摘要）"),
    k: int = Query(default=5, ge=1, le=MAX_K),
    defect_type: list[str] | None = Query(default=None, description="缺陷类型预过滤"),  # noqa: B008
    statute: list[str] | None = Query(default=None, description="条款预过滤"),  # noqa: B008
    tag: list[str] | None = Query(default=None, description="标签预过滤"),  # noqa: B008
    patent_type: str | None = Query(default=None, description="invention / utility_model / design"),
    user: dict[str, Any] = Depends(current_user),
) -> OaSearchOut:
    """元数据预过滤 → 向量 / 暴搜 / 关键词检索；无库或未配 embedding 时照常 200。"""
    filters: dict[str, Any] = {}
    if defect_type:
        filters["defect_types"] = [x for x in defect_type if x]
    if statute:
        filters["statutes"] = [x for x in statute if x]
    if tag:
        filters["tags"] = [x for x in tag if x]
    if patent_type:
        filters["patent_type"] = patent_type

    result = await vector.search(q, k=k, filters=filters, viewer=viewer_of(user))
    return OaSearchOut(
        retrieval_mode=result.get("mode") or "keyword",
        query=result.get("query") or "",
        k=int(result.get("k") or k),
        total_candidates=int(result.get("total_candidates") or 0),
        hits=result.get("hits") or [],
        filters=result.get("filters") or {},
        message=str(result.get("message") or ""),
        error=str(result.get("error") or ""),
    )


# ---------------------------------------------------------------------------
# 索引状态 / 全量重建
# ---------------------------------------------------------------------------


@router.get("/library/stats", response_model=OaLibraryStatsOut, summary="索引状态摘要（前端诊断用）")
async def library_stats() -> OaLibraryStatsOut:
    stats = await db.arun(vector.stats_sync)
    configured = await db.arun(vector.is_configured)
    mode = "keyword" if not configured else ("vector" if stats["vec_available"] else "brute")
    return OaLibraryStatsOut(
        **stats,
        embedding_configured=configured,
        retrieval_mode=mode,
        message="" if configured else await db.arun(vector.not_configured_message),
    )


#: 后台重建任务的进程内状态（本地单用户；进程重启即失，重跑一次即可）
_rebuild_state: dict[str, Any] = {
    "running": False, "started_at": "", "finished_at": "", "error": "", "result": None,
}
_rebuild_task: asyncio.Task | None = None


def rebuild_running() -> bool:
    """是否有存活的后台重建任务。"""
    return _rebuild_task is not None and not _rebuild_task.done()


def _confirmed_count_sync() -> int:
    """已确认（可进检索库）的案例数。"""
    row = db.query_one("SELECT COUNT(*) AS n FROM oa_library WHERE status='confirmed'")
    return int(row["n"]) if row else 0


async def _run_rebuild() -> None:
    """后台重建任务体：结果与错误写进 `_rebuild_state`，由 status 端点轮询。"""
    try:
        result = await oa_library.rebuild()
        _rebuild_state["result"] = result
        _rebuild_state["error"] = "；".join(str(e) for e in (result.get("errors") or []))
    except Exception as exc:  # 后台任务不得让异常逃逸
        logger.exception("案例库向量重建失败")
        _rebuild_state["error"] = f"{type(exc).__name__}: {exc}"
        _rebuild_state["result"] = None
    finally:
        _rebuild_state["running"] = False
        _rebuild_state["finished_at"] = db.now_str()


@router.post(
    "/library/rebuild",
    status_code=202,
    response_model=OaRebuildOut,
    dependencies=[Depends(require_admin)],
    summary="全量重建向量库（默认后台任务 + GET /oa/library/rebuild/status 轮询；wait=true 同步执行）",
)
async def rebuild_library(
    wait: bool = Query(
        default=False,
        description="true = 同步执行并返回真实结果（小语料/测试用）；false = 后台任务 + 轮询",
    ),
) -> OaRebuildOut:
    """DROP 向量表 → 清空向量 → 逐案例重新切块与嵌入（维度变更后必须调用）。"""
    if rebuild_running():
        raise HTTPException(status_code=409, detail="向量库重建任务正在运行中")

    cfg = await db.arun(vector.load_config)
    queued = await db.arun(_confirmed_count_sync)

    if wait:
        result = await oa_library.rebuild()
        _rebuild_state.update(
            {
                "running": False,
                "started_at": db.now_str(),
                "finished_at": db.now_str(),
                "error": "；".join(result.get("errors") or []),
                "result": result,
            }
        )
        return OaRebuildOut(
            ok=bool(result.get("ok")),
            queued=int(result.get("cases") or 0),
            dim=int(result.get("dim") or 0),
            mode=result.get("mode") or "keyword",
            vec_available=bool(result.get("vec_available")),
            message=str(result.get("message") or ""),
        )

    global _rebuild_task
    _rebuild_state.update(
        {"running": True, "started_at": db.now_str(), "finished_at": "", "error": "", "result": None}
    )
    _rebuild_task = asyncio.create_task(_run_rebuild(), name="oa-library-rebuild")
    return OaRebuildOut(
        ok=True,
        queued=queued,
        dim=int(cfg.dim or 0),
        mode=current_mode(),
        vec_available=vector.vec_available(),
        message=(
            f"已在后台启动向量库重建（{queued} 个已确认案例），"
            "请轮询 GET /oa/library/rebuild/status 获取进度与结果。"
        ),
    )


@router.get(
    "/library/rebuild/status",
    response_model=OaRebuildStatusOut,
    dependencies=[Depends(require_admin)],
    summary="后台重建任务状态（轮询）",
)
async def rebuild_status() -> OaRebuildStatusOut:
    return OaRebuildStatusOut(
        running=rebuild_running() or bool(_rebuild_state["running"]),
        started_at=str(_rebuild_state["started_at"] or ""),
        finished_at=str(_rebuild_state["finished_at"] or ""),
        error=str(_rebuild_state["error"] or ""),
        result=_rebuild_state["result"],
    )


# ---------------------------------------------------------------------------
# 单条：详情 / 人审更新 / 删除
# ---------------------------------------------------------------------------


@router.get("/library/{library_id}", response_model=OaLibraryDetail, summary="案例详情（含笔记正文、脱敏登记、检索块）")
async def get_case(
    library_id: str, user: dict[str, Any] = Depends(current_user)
) -> OaLibraryDetail:
    try:
        return await oa_library.get(library_id, viewer_of(user))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put(
    "/library/{library_id}",
    response_model=OaLibraryUpdateOut,
    summary="人审更新；status='confirmed' 触发切块 + 向量化，回落 draft 则清空检索块",
)
async def update_case(
    library_id: str,
    body: OaLibraryUpdateIn,
    user: dict[str, Any] = Depends(current_user),
) -> OaLibraryUpdateOut:
    if body.status is None and body.note_md is None and body.frontmatter is None:
        raise HTTPException(status_code=422, detail="请至少提供 status / note_md / frontmatter 之一")
    viewer = viewer_of(user)
    try:
        before = await oa_library.get(library_id, viewer)
        item, normalized = await oa_library.update(
            library_id, status=body.status, note_md=body.note_md,
            frontmatter=body.frontmatter, viewer=viewer,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    reindexed = (body.status is not None and body.status != before.status) or (
        body.note_md is not None and body.note_md != before.note_md
    )
    return OaLibraryUpdateOut(item=item, normalized=normalized, reindexed=reindexed)


@router.delete("/library/{library_id}", response_model=Ok, summary="删除案例（连带 chunks 与向量）")
async def delete_case(
    library_id: str, user: dict[str, Any] = Depends(current_user)
) -> Ok:
    try:
        await oa_library.delete(library_id, viewer_of(user))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Ok()
