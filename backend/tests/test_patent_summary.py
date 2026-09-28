"""查新条目摘要补全（patent_fetch.fetch_patent_summary / cnipa.enrich_hits）的测试。**不触网。**

起因：用户从 Google 上找到两篇与本案高度相关的专利（CN101656028A「纤维支气管镜训练箱」、
CN114186982A）手工补录进来，只填了标题和全文 PDF 链接。平台不去取内容，消化改写只能写
「该条无摘要……仅据标题保守概括」——最该认真比对的条目在 1.1 里只剩一句推测。

真站点取证（2026-09-28）得到的事实，钉在这里：
- Google Patents 详情页的 meta 里有摘要（DC.description）、申请人、公开日、PDF 直链；
- 连续十来个请求后整站回 503（限流），隔几秒通常恢复；
- CN101656028A（2010 年）的 PDF 是扫描件，没有文字层——老专利只能靠详情页的文字。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.services import cnipa, patent_fetch

API = "/api/v1"

PAGE_HTML = """<html><head>
<meta name="DC.title" content="支气管软镜训练方法、电子设备及存储介质
       ">
<meta name="DC.description" content="
     本发明公开了一种支气管软镜训练方法，包括：提供支气管软镜理论培训课程；通过支气管软镜操控换向训练装置的考核。">
<meta name="DC.contributor" content="李文献" scheme="inventor">
<meta scheme="assignee" name="DC.contributor" content="复旦大学附属眼耳鼻喉科医院">
<meta name="DC.date" content="2022-01-28" scheme="dateSubmitted">
<meta name="citation_pdf_url" content="https://patentimages.storage.googleapis.com/de/81/35/6c1474d79e79fd/CN114186982A.pdf">
</head><body><time itemprop="publicationDate" datetime="2022-03-15">2022-03-15</time></body></html>"""


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, sec: float) -> None:
        self.calls.append(sec)


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


def test_pub_no_from_url():
    assert patent_fetch.pub_no_from_url(
        "https://patentimages.storage.googleapis.com/26/5e/56/8acbc85b329e31/CN101656028A.pdf"
    ) == "CN101656028A"
    assert patent_fetch.pub_no_from_url("https://patents.google.com/patent/cn114186982a/zh") == "CN114186982A"
    assert patent_fetch.pub_no_from_url("https://example.com/x") == ""
    assert patent_fetch.pub_no_from_url(None) == ""


def test_parse_summary_page_reads_meta_in_any_attribute_order():
    info = patent_fetch.parse_summary_page(PAGE_HTML)
    assert info["title"] == "支气管软镜训练方法、电子设备及存储介质"
    assert info["abstract"].startswith("本发明公开了一种支气管软镜训练方法")
    assert info["applicant"] == "复旦大学附属眼耳鼻喉科医院"      # 取 assignee，不是发明人
    assert info["pub_date"] == "2022-03-15"
    assert info["pdf_url"].endswith("/CN114186982A.pdf")


def test_parse_summary_page_falls_back_to_abstract_section():
    html_text = '<section itemprop="abstract"><h2>Abstract</h2><div class="abstract">一种训练箱，包括箱体。</div></section>'
    assert patent_fetch.parse_summary_page(html_text)["abstract"] == "一种训练箱，包括箱体。"


def test_pdf_text_summary_rejects_scanned_pdf():
    """扫描件没有文字层：要说清原因，而不是返回一个空摘要装作成功。"""
    import fitz

    doc = fitz.open()
    doc.new_page()
    pdf = doc.tobytes()
    with pytest.raises(ValueError, match="扫描件"):
        patent_fetch.pdf_text_summary(pdf)


def test_pdf_text_summary_reads_front_page():
    import fitz

    front = (
        "(19)国家知识产权局\n(12)发明专利申请\n(21)申请号 202210107915.7\n(43)申请公布日 2022.03.15\n"
        "(71)申请人 复旦大学附属眼耳鼻喉科医院\n地址 200031 上海市徐汇区汾阳路83号\n"
        "(54)发明名称\n支气管软镜训练方法、电子设备及存储介质\n"
        "(57)摘要\n本发明公开了一种支气管软镜训练方法，包\n括：提供支气管软镜理论培训课程。\n"
    )
    doc = fitz.open()
    page = doc.new_page()
    try:
        page.insert_text((40, 60), front, fontname="china-s", fontsize=9)
    except Exception as exc:  # noqa: BLE001  # pragma: no cover —— 个别 PyMuPDF 构建不带内置 CJK 字体
        pytest.skip(f"PyMuPDF 无内置中文字体：{exc}")
    info = patent_fetch.pdf_text_summary(doc.tobytes())
    assert info["abstract"].startswith("本发明公开了一种支气管软镜训练方法，包括")  # 版面换行被接回
    assert info["applicant"] == "复旦大学附属眼耳鼻喉科医院"                       # 地址被剥掉
    assert info["title"] == "支气管软镜训练方法、电子设备及存储介质"


# ---------------------------------------------------------------------------
# fetch_patent_summary
# ---------------------------------------------------------------------------


async def test_summary_from_page():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text=PAGE_HTML)

    async with _client(handler) as client:
        s = await patent_fetch.fetch_patent_summary("CN114186982A", client=client)
    assert s.ok and s.source == "google_patents_page"
    assert s.applicant == "复旦大学附属眼耳鼻喉科医院" and s.pub_date == "2022-03-15"
    assert seen == ["https://patents.google.com/patent/CN114186982A/zh"]


async def test_summary_backs_off_on_rate_limit_then_succeeds():
    """503 是「这一刻请求太密」，不是「取不到」：退避重试，而不是直接报失败。"""
    replies = iter([503, 503, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        code = next(replies)
        return httpx.Response(code, text=PAGE_HTML if code == 200 else "unusual traffic")

    sleeps = Sleeps()
    async with _client(handler) as client:
        s = await patent_fetch.fetch_patent_summary("CN114186982A", client=client, sleep=sleeps)
    assert s.ok
    assert sleeps.calls == list(patent_fetch.RATE_LIMIT_RETRY_DELAYS)


async def test_summary_falls_back_to_pdf_when_page_is_rate_limited(monkeypatch: pytest.MonkeyPatch):
    pdf_url = "https://patentimages.storage.googleapis.com/de/81/35/6c1474d79e79fd/CN114186982A.pdf"

    def handler(request: httpx.Request) -> httpx.Response:
        if "patents.google.com" in str(request.url):
            return httpx.Response(503, text="unusual traffic")
        return httpx.Response(200, content=b"%PDF-1.4 fake")

    monkeypatch.setattr(
        patent_fetch,
        "pdf_text_summary",
        lambda _pdf: {"title": "T", "abstract": "从 PDF 扉页取到的摘要", "applicant": "A", "pub_date": ""},
    )
    async with _client(handler) as client:
        s = await patent_fetch.fetch_patent_summary(
            None, pdf_url=pdf_url, client=client, sleep=Sleeps()
        )
    assert s.pub_no == "CN114186982A"                 # 公开号从链接里认出来
    assert s.ok and s.source == "pdf_text"
    assert s.abstract == "从 PDF 扉页取到的摘要"


async def test_summary_failure_explains_every_source(monkeypatch: pytest.MonkeyPatch):
    """两条路都走不通：原因逐条写清（限流 + 扫描件），调用方据此告诉用户。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if "patents.google.com" in str(request.url):
            return httpx.Response(503, text="unusual traffic")
        return httpx.Response(200, content=b"%PDF-1.4 scanned")

    def scanned(_pdf: bytes) -> dict[str, str]:
        raise ValueError("PDF 为扫描件，没有可抽取的文字层")

    monkeypatch.setattr(patent_fetch, "pdf_text_summary", scanned)
    async with _client(handler) as client:
        s = await patent_fetch.fetch_patent_summary(
            "CN101656028A",
            pdf_url="https://patentimages.storage.googleapis.com/26/5e/56/8acbc85b329e31/CN101656028A.pdf",
            client=client,
            sleep=Sleeps(),
        )
    assert not s.ok
    assert "限流" in s.error and "扫描件" in s.error
    assert s.rate_limited


# ---------------------------------------------------------------------------
# cnipa.enrich_hits（真落库）
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _db(client: TestClient):
    return client


def _new_case(client: TestClient, title: str) -> str:
    resp = client.post(f"{API}/cases", json={"module": "disclosure", "title": title})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_enrich_hits_fills_missing_abstracts(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    case_id = _new_case(client, "补全-摘要")
    hits = await cnipa.add_manual_hits(
        case_id,
        [
            {"url": "https://patentimages.storage.googleapis.com/26/5e/56/8acbc85b329e31/CN101656028A.pdf",
             "pub_no": "CN101656028A", "title": "纤维支气管镜训练箱"},
            {"url": "https://x/CN1A", "pub_no": "CN1A", "title": "已有摘要", "abstract": "原有摘要"},
            {"url": "https://patents.google.com/patent/CN114186982A/zh", "title": "支气管软镜训练方法"},
        ],
    )
    calls: list[tuple[str, str | None]] = []

    async def fake_summary(pub_no, *, pdf_url=None, client=None, **_kw):
        calls.append((pub_no, pdf_url))
        if pub_no == "CN101656028A":
            return patent_fetch.PatentSummary(pub_no=pub_no, ok=True, abstract="一种纤维支气管镜训练箱，包括箱体……",
                                              applicant="某医院", pub_date="2010-02-24", source="google_patents_page")
        return patent_fetch.PatentSummary(pub_no=pub_no, error="Google Patents 详情页 HTTP 503（Google 限流）")

    monkeypatch.setattr(patent_fetch, "fetch_patent_summary", fake_summary)
    out, problems = await cnipa.enrich_hits(case_id, hits, spacing=0)

    # 已有摘要的不发请求；PDF 链接作为兜底传下去；公开号可从链接认出
    assert calls == [
        ("CN101656028A", "https://patentimages.storage.googleapis.com/26/5e/56/8acbc85b329e31/CN101656028A.pdf"),
        ("CN114186982A", None),
    ]
    assert [h.title for h in out] == [h.title for h in hits]          # 顺序不变
    assert out[0].abstract.startswith("一种纤维支气管镜训练箱")
    assert out[0].applicant == "某医院" and out[0].pub_date == "2010-02-24"
    assert out[1].abstract == "原有摘要"
    assert not out[2].abstract                                         # 没补上的原样返回
    assert problems == ["CN114186982A：Google Patents 详情页 HTTP 503（Google 限流）"]

    # 落库了：之后的列表 / 消化改写都能看到
    stored = {h.pub_no: h for h in await cnipa.list_hits(case_id)}
    assert stored["CN101656028A"].abstract.startswith("一种纤维支气管镜训练箱")


async def test_enrich_hits_is_noop_when_nothing_missing(monkeypatch: pytest.MonkeyPatch, client: TestClient):
    case_id = _new_case(client, "补全-无需")
    hits = await cnipa.add_manual_hits(case_id, [{"url": "https://x/CN2A", "abstract": "有"}])

    async def boom(*_a: Any, **_k: Any):  # pragma: no cover
        raise AssertionError("不该发请求")

    monkeypatch.setattr(patent_fetch, "fetch_patent_summary", boom)
    out, problems = await cnipa.enrich_hits(case_id, hits)
    assert out == hits and problems == []


async def test_enrich_stops_waiting_once_google_is_rate_limiting(monkeypatch: pytest.MonkeyPatch, client: TestClient):
    """代理出口 IP 被 Google 标记时退避救不回来：第一条退避完仍被限流，后面的只试一次。"""
    case_id = _new_case(client, "补全-熔断")
    hits = await cnipa.add_manual_hits(
        case_id,
        [{"url": f"https://patents.google.com/patent/CN10000000{i}A/zh", "title": f"条目{i}"} for i in range(3)],
    )
    delays_seen: list[tuple[float, ...]] = []

    async def limited(pub_no, *, pdf_url=None, client=None, retry_delays=(), **_kw):
        delays_seen.append(tuple(retry_delays))
        return patent_fetch.PatentSummary(pub_no=pub_no, error="Google Patents 详情页 HTTP 503", rate_limited=True)

    monkeypatch.setattr(patent_fetch, "fetch_patent_summary", limited)
    _out, problems = await cnipa.enrich_hits(case_id, hits, spacing=0)
    assert delays_seen == [patent_fetch.RATE_LIMIT_RETRY_DELAYS, (), ()]
    assert len(problems) == 3                                   # 补不上的每条都如实列出
