"""交付物（artifacts）的 API 契约模型（版本化只增不改）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

ArtifactKind = Literal[
    "disclosure_md", "disclosure_docx", "disclosure_pdf",
    "patent_content_json", "patent_docx", "patent_pdf",
    "figure_svg", "figure_png", "mermaid_png",
    "reader_note_md", "reader_note_docx",
    "oa_response_md", "oa_response_docx",
    "revision_log_md", "search_report_json",
]

IterationType = Literal["initial", "merge", "correction", "rebuild", "export"]


class ArtifactOut(BaseModel):
    """交付物记录。"""

    id: str
    case_id: str
    version: int
    kind: ArtifactKind
    filename: str
    stored_path: str
    run_group: str | None = None
    iteration_type: IterationType | None = None
    summary: str | None = None
    source_artifact_id: str | None = None
    created_at: str


class ArtifactExportIn(BaseModel):
    """POST /artifacts/{id}/export 请求体。"""

    format: Literal["docx", "pdf"]


class ArtifactContentOut(BaseModel):
    """GET /artifacts/{id}/content：文本类交付物全文预览。"""

    id: str
    kind: ArtifactKind
    version: int
    filename: str
    content: str
