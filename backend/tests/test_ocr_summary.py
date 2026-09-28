"""扫描件 OCR 补摘要（services/ocr + patent_fetch.parse_front_page_text）的测试。**不触网、不起 OCR。**

起因：CN101656028A（2010 年）的公开文本是扫描件，PDF 没有文字层；国知局公布公告的检索结果里
它也没有摘要；Google Patents 详情页是唯一的文字来源，而 Google 把本机代理的出口 IP 判成了
自动化流量，一律 503。系统自带的 OCR 引擎能把扉页认出来——下面的 OCR_TEXT 就是 2026-09-28
Windows OCR 对这份扉页的真实输出（汉字之间带空格、著录项编号的括号认成了方括号）。
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.services import cnipa, ocr, patent_fetch

API = "/api/v1"

OCR_TEXT = """[ 19 ] 中 华 人 民 共 和 国 国 家 知 识 产 权 局
[ 12 ] 发 明 专 利 申 请 公 布 说 明 书
[ 51 ] lnt. CI.
B23 刀 8 ． 似 丿
[ 43 〕 公 开 日 2010 年 2 月 24 日
[ 22 ] 申 请 日 2 開 9 ． 9 ． 21
[ 21 ] 申 请 号 2 開 910196 開 3 ， 6
[ 71 ] 申 请 人 朱 科 明
[ 21 ] 申 请 号 2 佣 910196003 ． 6
[ 74 ] 专 利 代 理 机 构
代 理 人
[ 11 ] 公 开 号 CN 101656028A
上 海 东 亚 专 利 商 标 代 理 有 限 公
司
陈 树 德
地 址 20 33 上 海 市 杨 浦 区 长 海 路 168 号
共 同 申 请 人 卞 金 俊 王 嘉 锋 华 昱 万 小 健
邓 小 明
[ 72 ] 发 明 人 朱 科 明 卞 金 俊 王 嘉 锋 华 昱
万 小 健 邓 小 明
权 利 要 求 书 1 页 说 明 书 6 页 附 图 5 页
[ 54 ] 发 明 名 称
纤 维 支 气 管 镜 训 练 箱
[ 57 ] 摘 要
本 发 明 公 开 一 种 纤 维 支 气 管 镜 训 练 箱 ， 包 括 一
箱 体 ， 箱 体 由 闭 合 的 上 箱 体 和 长 方 体 形 状 的 下 箱 体
构 成 ， 箱 体 顶 部 设 有 一 小 孔 ， 上 箱 体 为 模 拟 人 体 胸
腔 的 锥 形 体 形 状 ， 下 箱 体 设 有 开 口 ， 并 在 开 口 处 设
有 可 开 启 的 门 ， 上 箱 体 和 下 箱 体 内 横 向 设 置 若 干 互
相 平 行 的 隔 板 ， 隔 板 上 依 照 气 管 、 支 气 管 的 构 造 设
置 若 干 个 通 孔 ， 隔 板 与 箱 体 内 侧 壁 活 动 连 接 或 固 定
连 接 。 本 发 明 的 技 术 效 果 ： 结 构 简 单 ， 制 作 成 本 低
廉 ， 且 操 作 性 强 ， 易 于 普 及 推 广 ； 该 纤 维 支 气 管 镜
训 练 箱 高 度 模 拟 人 体 气 管 、 支 气 管 结 构 ， 小 孔 位 置
排 列 严 格 按 照 活 体 纤 维 支 气 管 镜 下 气 管 支 气 管 的 相
对 位 置 设 置 ， 设 计 科 学 合 理 ， 能 够 达 到 促 进 操 作 者
熟 练 掌 握 人 体 气 管 支 气 管 结 构 的 目 的 。"""

PDF_URL = "https://patentimages.storage.googleapis.com/26/5e/56/8acbc85b329e31/CN101656028A.pdf"


class Sleeps:
    async def __call__(self, _sec: float) -> None:
        return None


def _scanned(_pdf: bytes) -> dict[str, str]:
    raise ValueError("PDF 为扫描件，没有可抽取的文字层")


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _google_blocked(request: httpx.Request) -> httpx.Response:
    if "patents.google.com" in str(request.url):
        return httpx.Response(503, text="Sorry... automated queries")
    return httpx.Response(200, content=b"%PDF-1.4 scanned")


# ---------------------------------------------------------------------------
# services/ocr
# ---------------------------------------------------------------------------


def test_normalize_lines_removes_gaps_between_cjk_only():
    text = "纤 维 支 气 管 镜 训 练 箱\n[ 43 〕 公 开 日 2010 年 2 月 24 日\n[ 51 ] lnt. CI.\n"
    # 只去汉字旁边的空格：「[ 43」里的空格不动，「lnt. CI.」的空格也不动
    assert ocr.normalize_lines(text).splitlines() == ["纤维支气管镜训练箱", "[ 43〕公开日2010年2月24日", "[ 51 ] lnt. CI."]


def test_available_is_false_off_windows(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ocr.sys, "platform", "linux")
    ocr.available.cache_clear()
    try:
        assert ocr.available() is False
        assert "Windows" in ocr.unavailable_reason()
    finally:
        ocr.available.cache_clear()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows OCR 探测")
def test_available_needs_a_simplified_chinese_engine(monkeypatch: pytest.MonkeyPatch):
    def probe(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout=probe.langs, stderr="")

    monkeypatch.setattr(ocr, "_powershell", probe)
    ocr.available.cache_clear()
    try:
        probe.langs = "en-US\nzh-Hans-CN\n"
        assert ocr.available() is True
        ocr.available.cache_clear()
        probe.langs = "en-US\n"
        assert ocr.available() is False
        assert "语言包" in ocr.unavailable_reason()
    finally:
        ocr.available.cache_clear()


def test_image_text_normalizes_and_swallows_engine_errors(monkeypatch: pytest.MonkeyPatch, tmp_path):
    replies = {"rc": 0, "out": "纤 维 支 气 管 镜\n训 练 箱\n"}

    def run(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        assert "-Path" in args and str(tmp_path / "x.png") in args
        return subprocess.CompletedProcess(args, replies["rc"], stdout=replies["out"], stderr="读图失败")

    monkeypatch.setattr(ocr, "_powershell", run)
    assert ocr.image_text(tmp_path / "x.png") == "纤维支气管镜\n训练箱"
    replies["rc"] = 3
    assert ocr.image_text(tmp_path / "x.png") == ""             # 失败不抛，只是取不到


# ---------------------------------------------------------------------------
# patent_fetch.parse_front_page_text
# ---------------------------------------------------------------------------


def test_parse_front_page_text_from_real_windows_ocr_output():
    info = patent_fetch.parse_front_page_text(OCR_TEXT)
    assert info["title"] == "纤维支气管镜训练箱"
    assert info["abstract"].startswith("本发明公开一种纤维支气管镜训练箱，包括一箱体，箱体由闭合的上箱体")
    assert info["abstract"].endswith("熟练掌握人体气管支气管结构的目的。")
    assert " " not in info["abstract"]
    assert info["applicant"] == "朱科明"
    assert info["pub_date"] == "2010-02-24"


def test_parse_front_page_text_cuts_applicant_at_address_and_merged_columns():
    merged = "(71)申请人 某医院 (21)申请号 202210107915.7\n(54)实用新型名称\n一种训练箱\n(57)摘要\n" + "本实用新型公开了一种训练箱，箱体由顶板与侧板组成，顶板上设有穿接孔。"
    assert patent_fetch.parse_front_page_text(merged)["applicant"] == "某医院"
    with_address = "(71)申请人 某公司\n地址 200031 上海市徐汇区汾阳路83号\n(72)发明人 甲\n(57)摘要 " + "一" * 30
    assert patent_fetch.parse_front_page_text(with_address)["applicant"] == "某公司"


def test_parse_front_page_text_rejects_too_short_abstract():
    assert patent_fetch.parse_front_page_text("(57)摘要\n略。")["abstract"] == ""


# ---------------------------------------------------------------------------
# fetch_patent_summary：扫描件走 OCR
# ---------------------------------------------------------------------------


async def test_summary_uses_ocr_for_scanned_pdf_when_google_is_blocked(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(patent_fetch, "pdf_text_summary", _scanned)
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "pdf_front_page_text", lambda _pdf: OCR_TEXT)
    async with _client(_google_blocked) as client:
        s = await patent_fetch.fetch_patent_summary("CN101656028A", pdf_url=PDF_URL, client=client, sleep=Sleeps())
    assert s.ok and s.source == "pdf_ocr" and s.scanned
    assert s.title == "纤维支气管镜训练箱" and s.applicant == "朱科明" and s.pub_date == "2010-02-24"
    assert s.abstract.startswith("本发明公开一种纤维支气管镜训练箱")


async def test_summary_explains_when_no_ocr_engine(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(patent_fetch, "pdf_text_summary", _scanned)
    monkeypatch.setattr(ocr, "available", lambda: False)
    monkeypatch.setattr(ocr, "unavailable_reason", lambda: "本机没有中文 OCR 引擎（Windows 需安装「中文(简体)」语言包）")
    async with _client(_google_blocked) as client:
        s = await patent_fetch.fetch_patent_summary("CN101656028A", pdf_url=PDF_URL, client=client, sleep=Sleeps())
    assert not s.ok and s.scanned
    assert "限流" in s.error and "扫描件" in s.error and "语言包" in s.error


async def test_summary_reports_ocr_miss(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(patent_fetch, "pdf_text_summary", _scanned)
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "pdf_front_page_text", lambda _pdf: "")
    async with _client(_google_blocked) as client:
        s = await patent_fetch.fetch_patent_summary("CN101656028A", pdf_url=PDF_URL, client=client, sleep=Sleeps())
    assert not s.ok and "OCR 也没有识别出摘要" in s.error


# ---------------------------------------------------------------------------
# enrich_hits：告诉用户摘要是从哪来的
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _db(client: TestClient):
    return client


async def test_enrich_reports_the_source_of_each_abstract(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    resp = client.post(f"{API}/cases", json={"module": "disclosure", "title": "补全-来源"})
    case_id = resp.json()["id"]
    hits = await cnipa.add_manual_hits(case_id, [{"url": PDF_URL, "pub_no": "CN101656028A", "title": "纤维支气管镜训练箱"}])

    async def ocr_summary(pub_no: str, **_kw: Any):
        return patent_fetch.PatentSummary(pub_no=pub_no, ok=True, abstract="本发明公开一种纤维支气管镜训练箱……",
                                          applicant="朱科明", pub_date="2010-02-24", source="pdf_ocr", scanned=True)

    monkeypatch.setattr(patent_fetch, "fetch_patent_summary", ocr_summary)
    messages: list[str] = []

    async def on_progress(_stage: str, msg: str) -> None:
        messages.append(msg)

    out, problems = await cnipa.enrich_hits(case_id, hits, on_progress=on_progress, spacing=0)
    assert problems == [] and out[0].abstract.startswith("本发明公开")
    assert any("来源：扫描件 OCR" in m for m in messages)
