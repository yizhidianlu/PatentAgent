"""Google 拿不到时到国知局公布公告取单行本扉页图、OCR 认摘要（cnipa.enrich_hits 的兜底）。**不触网、不起浏览器、不起 OCR。**

场景：国知局检出的老文献（如 CN101656028，2010 年）在站上没有摘要、条目链接也不是 PDF；
Google Patents 又被限流。此时唯一的文字来源是公布公告的单行本阅读器给出的扉页图。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_ocr_summary import OCR_TEXT  # tests/ 在 pytest 的 sys.path 上（prepend 模式）

from app.services import cnipa, ocr, patent_fetch

API = "/api/v1"
EPUB_LINK = "http://epub.cnipa.gov.cn/patent/CN101656028"


@pytest.fixture(autouse=True)
def _db(client: TestClient):
    return client


def _case(client: TestClient, title: str) -> str:
    resp = client.post(f"{API}/cases", json={"module": "disclosure", "title": title})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _google_down(pub_no: str, **_kw: Any) -> patent_fetch.PatentSummary:
    return patent_fetch.PatentSummary(
        pub_no=pub_no, error="Google Patents 详情页 HTTP 503（Google 限流，稍后重试通常可恢复）", rate_limited=True
    )


def test_parse_front_page_protocol_keeps_success_and_failure_apart():
    lines = [
        "BROWSER: channel=chrome",
        'EPUB_HOME_READY: {"sec": 9.1}',
        'EPUB_PAGE_JSON: {"pub": "CN101656028A", "path": "x/101656028-p1.png", "pages": 13, "title": "纤维支气管镜训练箱"}',
        'EPUB_PAGE_FAIL_JSON: {"pub": "CN1A", "error": "公布公告里没有这个公开号"}',
        "EPUB_PAGES_DONE: 1",
    ]
    out = cnipa.parse_front_page_protocol(lines)
    assert out["CN101656028A"]["path"] == "x/101656028-p1.png" and out["CN101656028A"]["pages"] == 13
    assert out["CN1A"] == {"pub": "CN1A", "error": "公布公告里没有这个公开号"}


async def test_enrich_rescues_old_document_via_cnipa_front_page(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    case_id = _case(client, "扉页-兜底")
    hits = await cnipa.add_manual_hits(case_id, [{"url": EPUB_LINK, "pub_no": "CN101656028A", "title": "纤维支气管镜训练箱"}])
    png = tmp_path / "101656028-p1.png"
    png.write_bytes(b"\x89PNG fake")
    asked: list[list[str]] = []

    async def fake_pages(pubs, *, on_progress=None):
        asked.append(list(pubs))
        return {"CN101656028A": {"pub": "CN101656028A", "path": str(png), "pages": 13, "title": "纤维支气管镜训练箱"}}

    monkeypatch.setattr(patent_fetch, "fetch_patent_summary", _google_down)
    monkeypatch.setattr(cnipa, "fetch_front_pages", fake_pages)
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "image_text", lambda path, **_kw: OCR_TEXT if Path(path) == png else "")
    messages: list[str] = []

    async def on_progress(_stage: str, msg: str) -> None:
        messages.append(msg)

    out, problems = await cnipa.enrich_hits(case_id, hits, on_progress=on_progress, spacing=0)
    assert asked == [["CN101656028A"]]
    assert problems == []
    assert out[0].abstract.startswith("本发明公开一种纤维支气管镜训练箱")
    assert out[0].applicant == "朱科明" and out[0].pub_date == "2010-02-24"
    assert any("国知局公布公告取 1 篇" in m for m in messages)
    assert any("来源：国知局单行本扉页 OCR" in m for m in messages)
    stored = {h.pub_no: h for h in await cnipa.list_hits(case_id)}
    assert stored["CN101656028A"].abstract == out[0].abstract          # 落库了


async def test_enrich_explains_when_front_page_route_also_fails(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    case_id = _case(client, "扉页-失败")
    hits = await cnipa.add_manual_hits(case_id, [{"url": EPUB_LINK, "pub_no": "CN101656028A", "title": "纤维支气管镜训练箱"}])

    async def no_pages(pubs, *, on_progress=None):
        return {"CN101656028A": {"pub": "CN101656028A", "error": "单行本阅读器没有打开"}}

    monkeypatch.setattr(patent_fetch, "fetch_patent_summary", _google_down)
    monkeypatch.setattr(cnipa, "fetch_front_pages", no_pages)
    monkeypatch.setattr(ocr, "available", lambda: True)
    out, problems = await cnipa.enrich_hits(case_id, hits, spacing=0)
    assert not out[0].abstract
    assert problems == ["CN101656028A：Google Patents 详情页 HTTP 503（Google 限流，稍后重试通常可恢复）；国知局单行本：单行本阅读器没有打开"]


async def test_enrich_skips_front_page_route_without_ocr_engine(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """没有 OCR 引擎，扉页图拿回来也认不出来：不白跑一趟浏览器。"""
    case_id = _case(client, "扉页-无OCR")
    hits = await cnipa.add_manual_hits(case_id, [{"url": EPUB_LINK, "pub_no": "CN101656028A"}])

    async def boom(*_a: Any, **_k: Any):  # pragma: no cover
        raise AssertionError("不该去取扉页图")

    monkeypatch.setattr(patent_fetch, "fetch_patent_summary", _google_down)
    monkeypatch.setattr(cnipa, "fetch_front_pages", boom)
    monkeypatch.setattr(ocr, "available", lambda: False)
    _out, problems = await cnipa.enrich_hits(case_id, hits, spacing=0)
    assert len(problems) == 1 and "国知局单行本" not in problems[0]
