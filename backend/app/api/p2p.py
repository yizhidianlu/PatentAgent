"""论文转专利专属 API（backend-architecture.md §3.6）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/PUT | `/cases/{id}/p2p/content` | 读/改 PatentContent（PUT 触发 pydantic + 权项 lint 校验，产新版本） |
| POST | `/cases/{id}/p2p/drawings` | 重跑附图脚本（不做 LLM REPAIR，失败图直接降级为提示词） |
| POST | `/cases/{id}/p2p/build` | JSON → DOCX → PDF |
| GET | `/cases/{id}/p2p/image-prompts` | 每图 Image2 精修 Prompt + 原始模板 |

所有写操作与流水线共用 `pipelines/paper2patent.py` 里的同一段代码；流水线运行中
（engine.is_running）拒绝并发写，返回 409。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError

from ..db import database as db
from ..models.paper2patent import (
    P2pBuildOut,
    P2pContentIn,
    P2pContentOut,
    P2pDrawingsOut,
    P2pImagePromptsOut,
    PatentContent,
)
from ..pipelines import engine
from ..pipelines import paper2patent as p2p
from ..services import assets_loader, claims_lint
from ..services import drawings as drawings_service
from .deps import client_ip, current_user, resolve_case_sync

logger = logging.getLogger(__name__)

router = APIRouter(tags=["论文转专利"])


# ---------------------------------------------------------------------------
# 公共读写
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
    if row["module"] != "paper2patent":
        raise HTTPException(status_code=400, detail="该案件不是论文转专利案件")
    return row


def _case_state(row) -> dict[str, Any]:
    try:
        return json.loads(row["state_json"] or "{}")
    except json.JSONDecodeError:
        return {}


def _latest_content_artifact(case_id: str) -> sqlite3.Row | None:
    return db.query_one(
        "SELECT * FROM artifacts WHERE case_id=? AND kind='patent_content_json' "
        "ORDER BY version DESC LIMIT 1",
        (case_id,),
    )


def _load_content_sync(
    case_id: str,
    user: dict[str, Any],
    *,
    ip: str | None = None,
    write: bool = False,
) -> tuple[dict[str, Any], sqlite3.Row | None]:
    """当前内容契约：优先案件工作内存，其次最近一版 patent_content_json 文件。"""
    row = _get_case_row(case_id, user, ip=ip, write=write)
    state = _case_state(row)
    artifact = _latest_content_artifact(case_id)
    content = state.get("content")
    if isinstance(content, dict) and content:
        return content, artifact
    if artifact is not None:
        path = Path(artifact["stored_path"])
        if path.is_file() and path.suffix.lower() == ".json":
            # 交付物落盘后可能被外部改坏 / 截断：报 422 说明哪一版坏了，别让它变成 500
            try:
                parsed = json.loads(path.read_text(encoding="utf-8-sig"))
            except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
                logger.warning("专利内容契约交付物解析失败：%s（%s）", path, exc)
                raise HTTPException(
                    status_code=422,
                    detail=f"专利内容契约文件《{artifact['filename']}》已损坏，无法解析：{exc}",
                ) from exc
            if not isinstance(parsed, dict):
                raise HTTPException(
                    status_code=422,
                    detail=f"专利内容契约文件《{artifact['filename']}》不是 JSON 对象",
                )
            return parsed, artifact
    raise HTTPException(status_code=404, detail="该案件尚无专利内容契约（请先跑完规则校验步骤）")


def _merge_state_sync(case_id: str, patch: dict[str, Any]) -> None:
    """把补丁合并进 cases.state_json（读-改-写在同一事务内）。"""

    def op(conn: sqlite3.Connection) -> None:
        row = conn.execute("SELECT state_json FROM cases WHERE id=?", (case_id,)).fetchone()
        try:
            state = json.loads(row[0] or "{}") if row else {}
        except json.JSONDecodeError:
            state = {}
        state.update(patch)
        conn.execute(
            "UPDATE cases SET state_json=?, updated_at=? WHERE id=?",
            (json.dumps(state, ensure_ascii=False), db.now_str(), case_id),
        )

    db.transaction(op)


def _reject_if_running(case_id: str) -> None:
    if engine.is_running(case_id):
        raise HTTPException(status_code=409, detail="该案件的流水线正在运行中，请先取消或等待完成")


def _lint_content(content: dict[str, Any]) -> list[dict[str, Any]]:
    return claims_lint.lint_claims(
        [str(c) for c in content.get("claims") or []], str(content.get("invention_name") or "")
    )


def _validated_content(content: dict[str, Any]) -> PatentContent:
    """把工作内存里的 content 校验成契约模型；不完整时 422（而非 500）。

    `cases.state_json['content']` 由流水线写入，中途取消 / 步骤失败 / 旧版本遗留都可能
    留下缺字段的半成品。读接口若直接 model_validate 会抛 ValidationError → 500，
    前端只看到「服务器错误」而无从下手；改为 422 并列出缺失字段。
    """
    try:
        return PatentContent.model_validate(content)
    except ValidationError as exc:
        fields = sorted({".".join(str(x) for x in e["loc"]) for e in exc.errors()})
        raise HTTPException(
            status_code=422,
            detail={
                "message": "专利内容契约不完整或格式不符，请重跑规则校验步骤后再读取",
                "invalid_fields": fields,
            },
        ) from exc


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


@router.get(
    "/cases/{case_id}/p2p/content",
    response_model=P2pContentOut,
    summary="读取当前专利内容契约（含权项 lint 结果）",
)
async def get_content(
    case_id: str,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> P2pContentOut:
    content, artifact = await db.arun(
        _load_content_sync, case_id, user, ip=client_ip(request)
    )
    return P2pContentOut(
        content=_validated_content(content),
        artifact_id=artifact["id"] if artifact is not None else None,
        version=artifact["version"] if artifact is not None else None,
        filename=artifact["filename"] if artifact is not None else None,
        lint=_lint_content(content),
    )


@router.put(
    "/cases/{case_id}/p2p/content",
    response_model=P2pContentOut,
    summary="覆盖专利内容契约（pydantic + 权项 lint 校验通过后落新版本）",
)
async def put_content(
    case_id: str,
    body: P2pContentIn,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> P2pContentOut:
    await db.arun(_get_case_row, case_id, user, ip=client_ip(request), write=True)
    _reject_if_running(case_id)

    content = json.loads(body.content.model_dump_json())
    findings = _lint_content(content)
    errors = [f for f in findings if f["severity"] == "error"]
    if errors:
        raise HTTPException(
            status_code=422,
            detail={"message": "权利要求未通过确定性校验，请修正后再提交", "findings": errors},
        )

    artifact = await p2p.save_content_artifact(case_id, content, summary="用户编辑版本")
    await db.arun(
        _merge_state_sync,
        case_id,
        {"content": content, "content_artifact_id": artifact.id, "gaps": content.get("gaps") or []},
    )
    return P2pContentOut(
        content=PatentContent.model_validate(content),
        artifact_id=artifact.id,
        version=artifact.version,
        filename=artifact.filename,
        lint=findings,
    )


@router.post(
    "/cases/{case_id}/p2p/drawings",
    response_model=P2pDrawingsOut,
    summary="重跑附图脚本（SVG/PNG + 资产回写；失败图降级为图像模型提示词）",
)
async def rebuild_drawings(
    case_id: str,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> P2pDrawingsOut:
    content, _ = await db.arun(
        _load_content_sync, case_id, user, ip=client_ip(request), write=True
    )
    _reject_if_running(case_id)

    try:
        result = await p2p.regenerate_drawings(None, case_id, content, repair=False)
    except drawings_service.DrawingGenerationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    new_content = result["content"]
    await p2p.save_figure_artifacts(case_id, new_content)
    artifact = await p2p.save_content_artifact(case_id, new_content, summary="附图重跑")
    await db.arun(
        _merge_state_sync,
        case_id,
        {
            "content": new_content,
            "content_artifact_id": artifact.id,
            "gaps": new_content.get("gaps") or [],
        },
    )
    return P2pDrawingsOut(
        figures=new_content.get("drawing_assets") or [],
        degraded=result["degraded"],
        gaps=result["gaps"],
        artifact_id=artifact.id,
    )


@router.post(
    "/cases/{case_id}/p2p/build",
    response_model=P2pBuildOut,
    summary="生成 Word/PDF（JSON→DOCX→PDF，PDF 失败自动走图片版兜底）",
)
async def build_documents(
    case_id: str,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> P2pBuildOut:
    content, _ = await db.arun(
        _load_content_sync, case_id, user, ip=client_ip(request), write=True
    )
    _reject_if_running(case_id)
    try:
        result = await p2p.build_files(case_id, content)
    except p2p.BuildError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return P2pBuildOut(
        docx=result["docx"],
        pdf=result["pdf"],
        pdf_engine=result["pdf_engine"],
        pdf_error=result["pdf_error"] or result["docx_warning"],
        placeholders=result["placeholders"],
    )


@router.get(
    "/cases/{case_id}/p2p/image-prompts",
    response_model=P2pImagePromptsOut,
    summary="每幅附图的 Image2 精修提示词（附原始 Prompt 模板）",
)
async def image_prompts(
    case_id: str,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> P2pImagePromptsOut:
    content, _ = await db.arun(
        _load_content_sync, case_id, user, ip=client_ip(request)
    )
    try:
        template = assets_loader.get_text("prompts/paper2patent/drawing_image_prompt.md")
    except KeyError:  # pragma: no cover —— 资产缺失属部署问题
        template = ""
    return P2pImagePromptsOut(
        prompts=content.get("image_model_prompts") or [], template=template
    )
