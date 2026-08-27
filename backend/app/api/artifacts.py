"""交付物 API（M2）：全部版本列表 / 下载 / 文本预览 / 导出（md→docx、docx→pdf）。

导出（backend-architecture.md §3.8）：
- POST /artifacts/{id}/export {format: docx|pdf}，同步执行；
- md → docx：mermaid 预渲染 + md_to_docx.py（OMML 公式）；
- docx → pdf：Word COM → soffice 三级链（services/export_pdf）；
- md → pdf：md→docx→pdf 串联，仅登记最终 pdf（中间 docx 为临时件）；
- 产物走 services/artifacts 版本化落盘：新 artifact 行、iteration_type='export'、
  source_artifact_id 指向源交付物。
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from ulid import ULID

from ..config import get_config
from ..db import database as db
from ..models.artifact import ArtifactContentOut, ArtifactExportIn, ArtifactOut
from ..services import artifacts as artifacts_service
from ..services import export_docx as export_docx_service
from ..services import export_pdf as export_pdf_service
from .deps import client_ip, current_user, resolve_artifact_sync, resolve_case_sync

logger = logging.getLogger(__name__)

router = APIRouter(tags=["交付物"])

# 文本预览允许的扩展名
_TEXT_EXTS = {".md", ".json", ".txt", ".svg", ".yaml", ".yml"}

# md kind → docx kind 的导出映射（仅列 schema 中存在的目标 kind）
_MD_TO_DOCX_KIND = {
    "disclosure_md": "disclosure_docx",
    "reader_note_md": "reader_note_docx",
    "oa_response_md": "oa_response_docx",
}
# docx kind → pdf kind
_DOCX_TO_PDF_KIND = {
    "disclosure_docx": "disclosure_pdf",
    "patent_docx": "patent_pdf",
}
# md kind → pdf kind（经 md→docx→pdf 串联）
_MD_TO_PDF_KIND = {
    "disclosure_md": "disclosure_pdf",
}


def _row_to_artifact(row: sqlite3.Row) -> ArtifactOut:
    return ArtifactOut(**dict(row))


@router.get("/cases/{case_id}/artifacts", response_model=list[ArtifactOut],
            summary="案件交付物全部版本（kind 可过滤；按 kind、版本倒序）")
async def list_artifacts(
    case_id: str,
    request: Request,
    kind: str | None = Query(default=None, description="按交付物类型过滤"),
    user: dict[str, Any] = Depends(current_user),
) -> list[ArtifactOut]:
    ip = client_ip(request)

    def op() -> list[sqlite3.Row]:
        resolve_case_sync(case_id, user, ip=ip)
        if kind:
            return db.query_all(
                "SELECT * FROM artifacts WHERE case_id=? AND kind=? ORDER BY version DESC",
                (case_id, kind),
            )
        return db.query_all(
            "SELECT * FROM artifacts WHERE case_id=? ORDER BY kind ASC, version DESC",
            (case_id,),
        )

    rows = await db.arun(op)
    return [_row_to_artifact(r) for r in rows]


@router.get("/artifacts/{artifact_id}/download", summary="下载交付物")
async def download_artifact(
    artifact_id: str,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> FileResponse:
    row = await db.arun(resolve_artifact_sync, artifact_id, user, ip=client_ip(request))
    path = Path(row["stored_path"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="磁盘上找不到该交付物文件")
    return FileResponse(path, filename=row["filename"])


@router.get("/artifacts/{artifact_id}/content", response_model=ArtifactContentOut,
            summary="文本类交付物全文预览（md/json/txt/svg）")
async def artifact_content(
    artifact_id: str,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> ArtifactContentOut:
    row = await db.arun(resolve_artifact_sync, artifact_id, user, ip=client_ip(request))
    path = Path(row["stored_path"])
    if path.suffix.lower() not in _TEXT_EXTS:
        raise HTTPException(status_code=415, detail=f"{path.suffix} 为二进制交付物，请走 download 接口")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="磁盘上找不到该交付物文件")
    text = await db.arun(lambda: path.read_text(encoding="utf-8", errors="replace"))
    return ArtifactContentOut(
        id=row["id"], kind=row["kind"], version=row["version"],
        filename=row["filename"], content=text,
    )


@router.post("/artifacts/{artifact_id}/export", response_model=ArtifactOut,
             summary="导出：md→docx / docx→pdf / md→pdf（产新 artifact 行，同步执行）")
async def export_artifact(
    artifact_id: str,
    body: ArtifactExportIn,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> ArtifactOut:
    row = await db.arun(
        resolve_artifact_sync, artifact_id, user, ip=client_ip(request), write=True
    )
    src_kind: str = row["kind"]
    src_path = Path(row["stored_path"])
    if not src_path.is_file():
        raise HTTPException(status_code=404, detail="源交付物文件已不存在于磁盘")

    # 依源 kind + 目标格式决定导出链与目标 kind
    chain: str  # 'md_docx' | 'docx_pdf' | 'md_docx_pdf'
    if body.format == "docx":
        target_kind = _MD_TO_DOCX_KIND.get(src_kind)
        if target_kind is None:
            raise HTTPException(status_code=422, detail=f"kind={src_kind} 不支持导出为 docx")
        chain = "md_docx"
    else:  # pdf
        if src_kind in _DOCX_TO_PDF_KIND:
            target_kind = _DOCX_TO_PDF_KIND[src_kind]
            chain = "docx_pdf"
        elif src_kind in _MD_TO_PDF_KIND:
            target_kind = _MD_TO_PDF_KIND[src_kind]
            chain = "md_docx_pdf"
        else:
            raise HTTPException(status_code=422, detail=f"kind={src_kind} 不支持导出为 pdf")

    # 导出续用源文件的案件名（去时间戳），保证版本序列同名
    base_name = artifacts_service.strip_timestamp(row["filename"])
    cfg = get_config()
    workdir = cfg.tmp_dir / f"artifact_export_{ULID()}"
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        if chain == "md_docx":
            out = workdir / "out.docx"
            try:
                await export_docx_service.export_md_to_docx(src_path, out)
            except export_docx_service.DocxExportError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            payload = await db.arun(out.read_bytes)
            ext = "docx"
        elif chain == "docx_pdf":
            out = workdir / "out.pdf"
            try:
                await export_pdf_service.docx_to_pdf(src_path, out)
            except export_pdf_service.PdfExportError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            payload = await db.arun(out.read_bytes)
            ext = "pdf"
        else:  # md → docx → pdf 串联
            mid = workdir / "mid.docx"
            out = workdir / "out.pdf"
            try:
                await export_docx_service.export_md_to_docx(src_path, mid)
                await export_pdf_service.docx_to_pdf(mid, out)
            except (export_docx_service.DocxExportError, export_pdf_service.PdfExportError) as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            payload = await db.arun(out.read_bytes)
            ext = "pdf"

        return await artifacts_service.save_artifact(
            row["case_id"], target_kind, payload, ext,
            title=base_name,
            run_group=row["run_group"],
            iteration_type="export",
            source_artifact_id=row["id"],
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
