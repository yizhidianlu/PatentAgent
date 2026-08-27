"""文件的 API 契约模型（M2：上传 / 转换 / 内容读取）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

FileKind = Literal["upload", "converted_md", "extracted_figure", "manual"]


class FileOut(BaseModel):
    """文件记录。"""

    id: str
    case_id: str
    kind: FileKind
    orig_name: str
    mime: str | None = None
    size: int | None = None
    stored_path: str
    md_path: str | None = None
    meta: dict[str, Any] | None = None   # {digest, pages, figure_captions[], convert_error}
    created_at: str


class FileUploadResult(BaseModel):
    """POST /cases/{id}/files 逐文件返回项：文件记录 + md 预览前 2KB + 转换错误。"""

    file: FileOut
    md_preview: str | None = None        # 转换 md 前 2KB（无转换产物时为 None）
    convert_error: str | None = None     # 转换失败原因（成功为 None；失败不影响原件落盘）


class FileContentOut(BaseModel):
    """GET /files/{id}/content：转换 md（或文本类原件）全文。"""

    id: str
    orig_name: str
    content: str
