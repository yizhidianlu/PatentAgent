"""M2 文件管线测试：docx/pptx/pdf → md 转换、图片直存、下载/内容/删除。

用 tests/fixtures/examples/example_batch_job_scheduler/ 的真实 docx/pptx 转换；
PDF 用 pymupdf 现造一页再转换。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

FIXTURE_DOCS = (
    Path(__file__).resolve().parent
    / "fixtures" / "examples" / "example_batch_job_scheduler" / "knowledge" / "docs"
)



@pytest.fixture(scope="module")
def case_id(client: TestClient) -> str:
    resp = client.post("/api/v1/cases", json={"module": "disclosure", "title": "M2 转换测试案件"})
    assert resp.status_code == 201
    return resp.json()["id"]


def _upload(client: TestClient, case_id: str, path: Path, mime: str) -> dict:
    """上传单个文件，返回逐文件结果项。"""
    with path.open("rb") as fh:
        resp = client.post(
            f"/api/v1/cases/{case_id}/files",
            files=[("files", (path.name, fh, mime))],
        )
    assert resp.status_code == 201, resp.text
    items = resp.json()
    assert len(items) == 1
    return items[0]


def test_docx_to_md(client: TestClient, case_id: str) -> None:
    """真实 docx → md：产出非空且含正文关键词，附图抽取成功。"""
    item = _upload(
        client, case_id, FIXTURE_DOCS / "sample_architecture_review.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert item["convert_error"] is None
    assert item["md_preview"] and "批任务调度" in item["md_preview"]

    file_id = item["file"]["id"]
    content = client.get(f"/api/v1/files/{file_id}/content")
    assert content.status_code == 200
    text = content.json()["content"]
    assert len(text) > 200
    assert "调度器维护全局队列" in text          # 正文关键词
    assert item["file"]["meta"]["digest"]        # sha256 摘要已记录
    # mammoth 抽出的内嵌图片目录已记录且非空
    assert item["file"]["meta"].get("figure_count", 0) >= 1


def test_pptx_to_md(client: TestClient, case_id: str) -> None:
    """真实 pptx → md：逐页导出、含页面文本关键词。"""
    item = _upload(
        client, case_id, FIXTURE_DOCS / "sample_scheduler_deck.pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )
    assert item["convert_error"] is None
    assert item["md_preview"] and "第 1 页" in item["md_preview"]

    file_id = item["file"]["id"]
    text = client.get(f"/api/v1/files/{file_id}/content").json()["content"]
    assert "批任务调度方案评审" in text
    assert "异构节点资源错配" in text


def test_pdf_to_md(client: TestClient, case_id: str, tmp_path: Path) -> None:
    """pymupdf 现造一页 PDF → md：文本抽取 + 页数记录。"""
    import pymupdf

    pdf_path = tmp_path / "sample_scheduler_test.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Batch Scheduler Patent Test Document")
    page.insert_text((72, 108), "Figure 1 module relations")
    doc.save(str(pdf_path))
    doc.close()

    item = _upload(client, case_id, pdf_path, "application/pdf")
    assert item["convert_error"] is None, item["convert_error"]
    meta = item["file"]["meta"]
    assert meta["pages"] == 1
    # 图注候选行（Figure 1 …）应被识别并带页码
    captions = meta.get("figure_captions") or []
    assert any(c["page"] == 1 and "Figure 1" in c["text"] for c in captions)

    file_id = item["file"]["id"]
    text = client.get(f"/api/v1/files/{file_id}/content").json()["content"]
    assert "Batch Scheduler Patent Test Document" in text
    assert "## 第 1 页" in text


def test_image_direct_store(client: TestClient, case_id: str) -> None:
    """图片直存：无 md、无 convert_error，download 与原件字节一致。"""
    src = FIXTURE_DOCS / "sample_assets" / "sample_fig_modules.png"
    item = _upload(client, case_id, src, "image/png")
    assert item["convert_error"] is None
    assert item["md_preview"] is None
    assert item["file"]["md_path"] is None
    assert item["file"]["meta"]["category"] == "image"

    file_id = item["file"]["id"]
    resp = client.get(f"/api/v1/files/{file_id}/download")
    assert resp.status_code == 200
    assert resp.content == src.read_bytes()
    # 图片无可读文本 → content 404
    assert client.get(f"/api/v1/files/{file_id}/content").status_code == 404


def test_text_direct_store(client: TestClient, case_id: str) -> None:
    """md/代码直存：md_path 指向原件，content 可直接读全文。"""
    src = FIXTURE_DOCS / "architecture.md"
    item = _upload(client, case_id, src, "text/markdown")
    assert item["convert_error"] is None
    assert item["md_preview"]
    file_id = item["file"]["id"]
    text = client.get(f"/api/v1/files/{file_id}/content").json()["content"]
    assert text.strip()


def test_delete_file(client: TestClient, case_id: str, tmp_path: Path) -> None:
    """删除：记录消失、磁盘原件与转换产物清理。"""
    src = tmp_path / "to_delete.txt"
    src.write_text("删除测试内容", encoding="utf-8")
    item = _upload(client, case_id, src, "text/plain")
    file_id = item["file"]["id"]
    stored = Path(item["file"]["stored_path"])
    assert stored.is_file()

    assert client.delete(f"/api/v1/files/{file_id}").status_code == 200
    assert not stored.exists()
    assert client.get(f"/api/v1/files/{file_id}/download").status_code == 404


def test_convert_error_not_fatal(client: TestClient, case_id: str, tmp_path: Path) -> None:
    """坏 docx：转换失败但上传成功（201），convert_error 有值、原件仍落盘。"""
    bad = tmp_path / "broken.docx"
    bad.write_bytes(b"this is not a real docx file")
    item = _upload(
        client, case_id, bad,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert item["convert_error"]                 # 有明确错误信息
    assert item["file"]["md_path"] is None
    assert Path(item["file"]["stored_path"]).is_file()
