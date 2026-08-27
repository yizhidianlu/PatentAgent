"""文件 API（M2）：上传（含同步转换）/ 下载 / 转换文本 / 删除。

行为（backend-architecture.md §3.3）：
- POST /cases/{id}/files：multipart 多文件；逐个落盘 data/uploads/{case_id}/ 并
  同步转换（docx/pptx 子进程、pdf 走 pymupdf、图片与文本直存），插 files 表，
  返回每个文件的记录 + md 预览前 2KB + convert_error（转换失败不失败整个请求）；
- GET /files/{id}/download：原件下载；
- GET /files/{id}/content：转换 md（或文本类原件）全文；
- DELETE /files/{id}：删记录并清理磁盘（原件 / 转换 md / 抽取图片目录）。
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from ulid import ULID

from ..config import get_config
from ..db import database as db
from ..models.common import Ok
from ..models.file import FileContentOut, FileOut, FileUploadResult
from ..services import convert as convert_service
from .deps import client_ip, current_user, resolve_case_sync, resolve_file_sync

logger = logging.getLogger(__name__)

router = APIRouter(tags=["文件"])

# md 预览截断（字节）
PREVIEW_BYTES = 2048


def _parse_meta(text: str | None) -> dict | None:
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _row_to_file(row: sqlite3.Row) -> FileOut:
    return FileOut(
        id=row["id"], case_id=row["case_id"], kind=row["kind"],
        orig_name=row["orig_name"], mime=row["mime"], size=row["size"],
        stored_path=row["stored_path"], md_path=row["md_path"],
        meta=_parse_meta(row["meta_json"]), created_at=row["created_at"],
    )


def _md_preview(md_path: str | None) -> str | None:
    """读转换 md 的前 2KB（按字节截断后 UTF-8 宽容解码）。"""
    if not md_path:
        return None
    try:
        with open(md_path, "rb") as fh:
            return fh.read(PREVIEW_BYTES).decode("utf-8", errors="ignore")
    except OSError:
        return None


def _save_and_convert(case_id: str, orig_name: str, mime: str | None, payload: bytes) -> FileUploadResult:
    """单个文件的同步处理：落盘 → 转换 → 插 files 表 → 组装返回项。"""
    cfg = get_config()
    case_dir = cfg.uploads_dir / case_id
    case_dir.mkdir(parents=True, exist_ok=True)

    stored_path = convert_service.allocate_upload_path(case_dir, orig_name)
    stored_path.write_bytes(payload)

    result = convert_service.convert_upload(case_id, stored_path)
    md_path = str(result.md_path) if result.md_path is not None else None
    convert_error = result.meta.get("convert_error")

    file_id = str(ULID())
    now = db.now_str()
    db.execute(
        """
        INSERT INTO files(id, case_id, kind, orig_name, mime, size, stored_path, md_path, meta_json, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            file_id, case_id, "upload", orig_name, mime, len(payload),
            str(stored_path), md_path,
            json.dumps(result.meta, ensure_ascii=False), now,
        ),
    )
    row = db.query_one("SELECT * FROM files WHERE id=?", (file_id,))
    return FileUploadResult(
        file=_row_to_file(row),
        md_preview=_md_preview(md_path),
        convert_error=convert_error,
    )


@router.post("/cases/{case_id}/files", response_model=list[FileUploadResult], status_code=201,
             summary="上传多文件（同步转换：docx/pptx/pdf→md，图片/文本直存；失败记 convert_error）")
async def upload_files(
    case_id: str,
    files: list[UploadFile],
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> list[FileUploadResult]:
    if not files:
        raise HTTPException(status_code=422, detail="未收到任何文件")
    await db.arun(resolve_case_sync, case_id, user, ip=client_ip(request), write=True)

    results: list[FileUploadResult] = []
    for f in files:
        payload = await f.read()
        orig_name = convert_service.sanitize_filename(f.filename or "unnamed")
        # 转换含子进程调用，逐个丢线程池执行（顺序处理，转换失败不影响其余文件）
        item = await db.arun(_save_and_convert, case_id, orig_name, f.content_type, payload)
        results.append(item)
    return results


@router.get("/files/{file_id}/download", summary="下载原件")
async def download_file(
    file_id: str,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> FileResponse:
    row = await db.arun(resolve_file_sync, file_id, user, ip=client_ip(request))
    path = Path(row["stored_path"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="磁盘上找不到该文件（可能已被移动或删除）")
    return FileResponse(path, filename=row["orig_name"], media_type=row["mime"] or "application/octet-stream")


@router.get("/files/{file_id}/content", response_model=FileContentOut, summary="读取转换 md（或文本类原件）全文")
async def file_content(
    file_id: str,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> FileContentOut:
    row = await db.arun(resolve_file_sync, file_id, user, ip=client_ip(request))
    if not row["md_path"]:
        raise HTTPException(status_code=404, detail="该文件没有可读文本（未转换或为二进制文件）")
    path = Path(row["md_path"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="转换文本已不存在于磁盘")
    text = await db.arun(lambda: path.read_text(encoding="utf-8", errors="replace"))
    return FileContentOut(id=row["id"], orig_name=row["orig_name"], content=text)


@router.delete("/files/{file_id}", response_model=Ok, summary="删除文件（记录 + 磁盘原件/转换产物）")
async def delete_file(
    file_id: str,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> Ok:
    ip = client_ip(request)

    def op() -> None:
        row = resolve_file_sync(file_id, user, ip=ip, write=True)
        meta = _parse_meta(row["meta_json"]) or {}
        cfg = get_config()
        uploads_root = cfg.uploads_dir.resolve()

        def safe_remove(raw: str | None, *, is_dir: bool = False) -> None:
            """仅允许清理 uploads/ 之下的路径，防误删。"""
            if not raw:
                return
            p = Path(raw)
            try:
                p.resolve().relative_to(uploads_root)
            except ValueError:
                logger.warning("拒绝删除 uploads 目录之外的路径：%s", raw)
                return
            if is_dir:
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)

        safe_remove(row["stored_path"])
        if row["md_path"] and row["md_path"] != row["stored_path"]:
            safe_remove(row["md_path"])
        safe_remove(meta.get("media_dir"), is_dir=True)
        db.execute("DELETE FROM files WHERE id=?", (file_id,))

    await db.arun(op)
    return Ok()
