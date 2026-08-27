"""CNIPA 查新服务与 API 测试（prompt-porting-spec §2 A4 / R8）。

**不依赖真实网络**：把 `app.services.cnipa.run_tool` 换成脚本化假子进程，按移植脚本
`tools/cnipa_epub_search.py` 的真实 stdout 协议（唯一一行 `EPUB_HITS_JSON:` + JSON 数组）
喂数据，其余环节（解析 → 落库 → 缓存 → 降级 → 人工兜底 → REST 契约）全部真跑。

覆盖：
- 解析入库（URL 照抄 link；无 link 条目丢弃）；
- 6 小时缓存命中（同案件复用旧命中；跨案件复制一份）；
- 失败降级：超时 / 退出码非零 / 无 EPUB_HITS_JSON / 零解析 —— 一律 status='failed'
  且**不抛异常**（交 A4 三选项门控）；
- 人工兜底录入、勾选、跳过查新、URL 白名单；
- 浏览器探测；
- REST：POST/GET/PATCH 五个端点。
"""

from __future__ import annotations

import json
import subprocess
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.models.search import ManualHitIn
from app.services import cnipa

API = "/api/v1"


# ---------------------------------------------------------------------------
# 脚手架
# ---------------------------------------------------------------------------



@pytest.fixture(autouse=True)
def _db(client: TestClient):
    """所有用例都要求数据库已初始化（服务层用例也经此 fixture 拉起 lifespan）。"""
    return client


def _new_case(client: TestClient, title: str) -> str:
    resp = client.post(f"{API}/cases", json={"module": "disclosure", "title": title})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


HITS_PAYLOAD: list[dict[str, Any]] = [
    {
        "title": "一种基于资源画像的任务调度方法",
        "pub_number": "CN114567890A",
        "link": "http://epub.cnipa.gov.cn/patent/CN114567890A",
        "abstract": "本发明公开了一种基于资源画像的任务调度方法，包括采集节点资源指标…",
    },
    {
        "title": "分布式集群的负载均衡装置",
        "pub_number": "CN113456789B",
        "link": "http://epub.cnipa.gov.cn/patent/CN113456789B",
        "abstract": None,
    },
    {
        # 无 link：URL 硬规则要求丢弃（1.1 每条须附可核验链接）
        "title": "无链接的脏条目",
        "pub_number": "CN112345678A",
        "link": None,
        "abstract": "…",
    },
]


def _proc(stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["fake"], returncode=returncode, stdout=stdout, stderr=stderr)


def _hits_stdout(hits: list[dict[str, Any]]) -> str:
    return "EPUB_HITS_JSON: " + json.dumps(hits, ensure_ascii=False) + "\n"


class FakeRunTool:
    """记录调用并返回预置结果的假 run_tool。"""

    def __init__(self, result: Any = None, raises: BaseException | None = None) -> None:
        self.result = result
        self.raises = raises
        self.calls: list[tuple[str, list[str], dict[str, Any]]] = []

    def __call__(self, script: str, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append((script, list(args), kwargs))
        if self.raises is not None:
            raise self.raises
        return self.result  # type: ignore[return-value]


def _patch_tool(monkeypatch: pytest.MonkeyPatch, fake: FakeRunTool) -> FakeRunTool:
    monkeypatch.setattr(cnipa, "run_tool", fake)
    return fake


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


def test_normalize_terms_splits_dedupes_and_caps():
    """检索词：按空白切分、有序去重、上限 8（脚本超 8 会以退出码 2 拒绝）。"""
    assert cnipa.normalize_terms(["资源画像 任务调度", "资源画像", " ", "限频重排"]) == [
        "资源画像",
        "任务调度",
        "限频重排",
    ]
    assert len(cnipa.normalize_terms([f"词{i}" for i in range(20)])) == 8
    assert cnipa.normalize_terms(None) == []


def test_normalize_type_maps_aliases():
    """类型映射到脚本 --type 的规范名；未知一律 all，绝不抛。"""
    assert cnipa.normalize_type("invention") == "invention"
    assert cnipa.normalize_type("实用新型") == "utility_model"
    assert cnipa.normalize_type("design") == "design"
    assert cnipa.normalize_type(None) == "all"
    assert cnipa.normalize_type("胡说八道") == "all"


def test_parse_hits_stdout_protocol():
    """只认 EPUB_HITS_JSON 那一行；缺行或非法 JSON 返回 None。"""
    stdout = "EPUB_NOTE: html_bytes=1024 disk=0\n" + _hits_stdout(HITS_PAYLOAD)
    parsed = cnipa.parse_hits_stdout(stdout)
    assert parsed is not None and len(parsed) == 3
    assert cnipa.parse_hits_stdout("EPUB_NOTE: nothing here") is None
    assert cnipa.parse_hits_stdout("EPUB_HITS_JSON: {不是JSON") is None


def test_normalize_hits_drops_linkless_and_dedupes():
    """无 link 条目丢弃、按 url 去重；url 逐字照抄 link。"""
    hits, dropped = cnipa.normalize_hits([*HITS_PAYLOAD, HITS_PAYLOAD[0]])
    assert dropped == 1
    assert len(hits) == 2
    assert hits[0]["url"] == "http://epub.cnipa.gov.cn/patent/CN114567890A"
    assert hits[0]["pub_no"] == "CN114567890A"
    assert hits[1]["abstract"] is None


def test_terms_key_order_insensitive():
    """缓存键与词序无关，但与类型有关。"""
    a = cnipa.terms_key(["甲", "乙"], "invention")
    assert a == cnipa.terms_key(["乙", "甲"], "invention")
    assert a != cnipa.terms_key(["甲", "乙"], "design")


# ---------------------------------------------------------------------------
# 检索主流程
# ---------------------------------------------------------------------------


async def test_search_parses_and_persists(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """成功路径：解析 → 落库 → 进度回调；query 行 done，命中 URL 照抄。"""
    case_id = _new_case(client, "查新-成功")
    fake = _patch_tool(monkeypatch, FakeRunTool(_proc(stdout=_hits_stdout(HITS_PAYLOAD))))

    stages: list[tuple[str, str]] = []

    async def on_progress(stage: str, msg: str) -> None:
        stages.append((stage, msg))

    result = await cnipa.search(
        case_id, ["资源画像 任务调度"], "invention", on_progress=on_progress
    )

    assert result.status == "done"
    assert result.ok is True
    assert result.error is None
    assert result.cached is False
    assert [h.url for h in result.hits] == [
        "http://epub.cnipa.gov.cn/patent/CN114567890A",
        "http://epub.cnipa.gov.cn/patent/CN113456789B",
    ]
    assert result.hits[0].abstract.startswith("本发明公开了")
    assert result.hits[0].selected is True
    assert result.hits[0].manual_entry is False

    # 子进程参数：--type 映射 + 一次会话传多词
    script, args, kwargs = fake.calls[0]
    assert script == "cnipa_epub_search.py"
    assert args == ["--type", "invention", "资源画像", "任务调度"]
    assert kwargs["timeout"] == cnipa.SEARCH_TIMEOUT
    assert "EPUB_WAF_MAX_WAIT_SEC" in kwargs["extra_env"]

    # 进度回调覆盖关键阶段
    assert [s for s, _ in stages] == ["start", "running", "parsed", "done"]

    # 落库
    queries = await cnipa.list_queries(case_id)
    assert len(queries) == 1
    assert queries[0].status == "done"
    assert queries[0].source == "cnipa"
    assert queries[0].terms == ["资源画像", "任务调度"]
    assert queries[0].hit_count == 2
    hits = await cnipa.list_hits(case_id)
    assert len(hits) == 2


async def test_search_reuses_cache_in_same_case(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """同案件 6 小时内同 terms+type：直接复用，不再起子进程。"""
    case_id = _new_case(client, "查新-缓存同案")
    fake = _patch_tool(monkeypatch, FakeRunTool(_proc(stdout=_hits_stdout(HITS_PAYLOAD))))

    first = await cnipa.search(case_id, ["缓存词甲", "缓存词乙"], "invention")
    assert first.status == "done" and first.cached is False
    assert len(fake.calls) == 1

    second = await cnipa.search(case_id, ["缓存词乙", "缓存词甲"], "invention")
    assert second.status == "done"
    assert second.cached is True
    assert len(fake.calls) == 1                     # 未再起子进程
    assert {h.url for h in second.hits} == {h.url for h in first.hits}
    assert len(await cnipa.list_hits(case_id)) == 2  # 未重复插入


async def test_search_cache_copies_across_cases(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """跨案件命中缓存：复制一份进新案件（仍不起子进程）。"""
    case_a = _new_case(client, "查新-缓存源案")
    case_b = _new_case(client, "查新-缓存目标案")
    fake = _patch_tool(monkeypatch, FakeRunTool(_proc(stdout=_hits_stdout(HITS_PAYLOAD))))

    await cnipa.search(case_a, ["跨案词甲", "跨案词乙"], "utility_model")
    assert len(fake.calls) == 1

    result = await cnipa.search(case_b, ["跨案词甲", "跨案词乙"], "utility_model")
    assert result.cached is True
    assert len(fake.calls) == 1
    assert len(result.hits) == 2
    assert all(h.case_id == case_b for h in result.hits)
    queries = await cnipa.list_queries(case_b)
    assert queries[0].cached is True


async def test_search_cache_disabled(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """use_cache=False：即使有缓存也重跑（用户点「重试」的语义）。"""
    case_id = _new_case(client, "查新-禁用缓存")
    fake = _patch_tool(monkeypatch, FakeRunTool(_proc(stdout=_hits_stdout(HITS_PAYLOAD))))

    await cnipa.search(case_id, ["禁缓存词"], "invention")
    await cnipa.search(case_id, ["禁缓存词"], "invention", use_cache=False)
    assert len(fake.calls) == 2


async def test_search_timeout_degrades(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """超时（疑似 WAF）：返回 failed，不抛异常，query 行落 failed + 原因。"""
    case_id = _new_case(client, "查新-超时")
    _patch_tool(
        monkeypatch,
        FakeRunTool(raises=subprocess.TimeoutExpired(cmd="cnipa_epub_search.py", timeout=180)),
    )

    stages: list[str] = []
    result = await cnipa.search(
        case_id, ["超时词"], "invention", on_progress=lambda stage, msg: stages.append(stage)
    )

    assert result.status == "failed"
    assert result.ok is False
    assert "超时" in result.error
    assert result.hits == []
    assert stages[-1] == "failed"

    queries = await cnipa.list_queries(case_id)
    assert queries[0].status == "failed"
    assert "超时" in queries[0].error
    assert await cnipa.list_hits(case_id) == []


async def test_search_nonzero_exit_degrades(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """脚本非零退出（playwright 缺失等）：failed + stderr 关键行入 error。"""
    case_id = _new_case(client, "查新-退出码")
    _patch_tool(
        monkeypatch,
        FakeRunTool(_proc(stderr="ERROR: pip install playwright\nHINT: browser.py --probe", returncode=1)),
    )
    result = await cnipa.search(case_id, ["退出码词"], "invention")
    assert result.status == "failed"
    assert "退出码 1" in result.error
    assert "playwright" in result.error


async def test_search_missing_marker_degrades(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """退出码 0 但没有 EPUB_HITS_JSON（页面改版/被拦截）：仍判 failed。"""
    case_id = _new_case(client, "查新-无标记")
    _patch_tool(monkeypatch, FakeRunTool(_proc(stdout="不是机读协议的输出", stderr="EPUB_NOTE: html_bytes=512")))
    result = await cnipa.search(case_id, ["无标记词"], "invention")
    assert result.status == "failed"
    assert "EPUB_HITS_JSON" in result.error


async def test_search_zero_hits_degrades(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """零解析：判 failed 交人工兜底（A4 明确禁止编造检索结果）。"""
    case_id = _new_case(client, "查新-零命中")
    _patch_tool(monkeypatch, FakeRunTool(_proc(stdout=_hits_stdout([]))))
    result = await cnipa.search(case_id, ["零命中词"], "invention")
    assert result.status == "failed"
    assert "未解析到任何命中" in result.error
    queries = await cnipa.list_queries(case_id)
    assert queries[0].status == "failed"

    # empty_is_failure=False 时按 done 处理（供上层按需放宽）
    case2 = _new_case(client, "查新-零命中放宽")
    result2 = await cnipa.search(case2, ["零命中词2"], "invention", empty_is_failure=False)
    assert result2.status == "done"
    assert result2.hits == []


async def test_search_empty_terms(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """空检索词：直接 failed，不起子进程也不写库。"""
    case_id = _new_case(client, "查新-空词")
    fake = _patch_tool(monkeypatch, FakeRunTool(_proc(stdout=_hits_stdout(HITS_PAYLOAD))))
    result = await cnipa.search(case_id, ["  "], "invention")
    assert result.status == "failed"
    assert result.error == "检索词为空"
    assert fake.calls == []
    assert await cnipa.list_queries(case_id) == []


# ---------------------------------------------------------------------------
# 人工兜底与命中管理
# ---------------------------------------------------------------------------


async def test_manual_hits_and_selection(client: TestClient):
    """人工录入：manual_entry=1、可勾选、可回写消化摘要、进 URL 白名单。"""
    case_id = _new_case(client, "查新-人工录入")
    hits = await cnipa.add_manual_hits(
        case_id,
        [
            ManualHitIn(
                url="https://patents.google.com/patent/CN109876543A",
                pub_no="CN109876543A",
                title="用户粘贴的在先文献",
                abstract="用户从别处找到的对比文件摘要。",
            ),
            {"url": "https://worldwide.espacenet.com/patent/EP1234567A1", "title": "另一篇"},
        ],
        note="A4 失败后用户粘贴",
    )
    assert len(hits) == 2
    assert all(h.manual_entry is True for h in hits)
    assert all(h.selected is True for h in hits)

    queries = await cnipa.list_queries(case_id)
    assert queries[0].source == "manual"
    assert queries[0].status == "done"

    # 重复 URL 不重复插入
    again = await cnipa.add_manual_hits(
        case_id, [{"url": "https://patents.google.com/patent/CN109876543A"}]
    )
    assert len(again) == 1
    assert len(await cnipa.list_hits(case_id)) == 2

    # 勾选 / 消化摘要
    target = hits[1]
    off = await cnipa.set_selected(target.id, False)
    assert off.selected is False
    assert len(await cnipa.list_hits(case_id, selected_only=True)) == 1
    digested = await cnipa.set_digest(hits[0].id, "消化改写后的方案概括。")
    assert digested.digest == "消化改写后的方案概括。"

    # URL 白名单（1.1 写作 lint 用）：默认只含已勾选的
    urls = await cnipa.hit_urls(case_id)
    assert urls == {"https://patents.google.com/patent/CN109876543A"}
    assert len(await cnipa.hit_urls(case_id, selected_only=False)) == 2

    with pytest.raises(KeyError):
        await cnipa.set_selected("不存在的id", True)


async def test_manual_hit_requires_url(client: TestClient):
    """人工录入必须带 URL（1.1 每条附可核验链接）。"""
    case_id = _new_case(client, "查新-缺URL")
    with pytest.raises(ValueError):
        await cnipa.add_manual_hits(case_id, [{"title": "没有链接"}])
    with pytest.raises(ValueError):
        await cnipa.add_manual_hits(case_id, [])


async def test_skip_search_records_manual_pending(client: TestClient):
    """跳过查新：记 manual_pending 会话，1.1 须如实写明未检索。"""
    case_id = _new_case(client, "查新-跳过")
    query = await cnipa.skip_search(case_id, "内网环境无法访问国知局")
    assert query.status == "manual_pending"
    assert query.skipped is True
    latest = await cnipa.latest_query(case_id)
    assert latest is not None and latest.id == query.id


# ---------------------------------------------------------------------------
# 浏览器探测
# ---------------------------------------------------------------------------


def test_probe_browser_parses_json(monkeypatch: pytest.MonkeyPatch):
    """--probe 的一行 JSON 被解析为 BrowserProbe（优先本机 Chrome）。"""
    payload = {"playwright": True, "channel": "chrome", "ok": True, "error": None, "hint": None}
    _patch_tool(monkeypatch, FakeRunTool(_proc(stdout=json.dumps(payload) + "\n", stderr="PROBE: ok=true")))
    probe = cnipa.probe_browser_sync()
    assert probe.ok is True
    assert probe.channel == "chrome"


def test_probe_browser_failure_is_graceful(monkeypatch: pytest.MonkeyPatch):
    """探测超时/失败不抛异常，返回 ok=False + 原因。"""
    _patch_tool(monkeypatch, FakeRunTool(raises=subprocess.TimeoutExpired(cmd="browser.py", timeout=120)))
    assert cnipa.probe_browser_sync().ok is False

    _patch_tool(
        monkeypatch,
        FakeRunTool(_proc(stderr="PROBE: ok=false channel= error=no browser", returncode=1)),
    )
    probe = cnipa.probe_browser_sync()
    assert probe.ok is False
    assert "no browser" in (probe.error or "")


# ---------------------------------------------------------------------------
# REST 契约
# ---------------------------------------------------------------------------


def _wait_for_search(client: TestClient, case_id: str, timeout: float = 20.0) -> dict[str, Any]:
    """轮询到最近一次会话不再是 running。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"{API}/cases/{case_id}/search/hits").json()
        if body["latest_status"] not in (None, "running"):
            return body
        time.sleep(0.05)
    raise AssertionError("查新后台任务未在超时内结束")


def test_api_search_flow(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """REST：触发检索（202）→ 后台完成 → 命中列表 → 勾选。"""
    case_id = _new_case(client, "查新-API成功")
    _patch_tool(monkeypatch, FakeRunTool(_proc(stdout=_hits_stdout(HITS_PAYLOAD))))

    resp = client.post(
        f"{API}/cases/{case_id}/search/cnipa",
        json={"terms": ["API词甲", "API词乙"], "patent_type": "invention"},
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == "running"
    assert resp.json()["terms"] == ["API词甲", "API词乙"]

    body = _wait_for_search(client, case_id)
    assert body["latest_status"] == "done"
    assert body["count"] == 2
    assert body["selected_count"] == 2
    assert body["queries"][0]["source"] == "cnipa"

    hit_id = body["hits"][0]["id"]
    patched = client.patch(f"{API}/search/hits/{hit_id}", json={"selected": False})
    assert patched.status_code == 200
    assert patched.json()["selected"] is False
    assert client.get(f"{API}/cases/{case_id}/search/hits?selected_only=true").json()["count"] == 1


def test_api_search_failure_is_reported_not_500(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """检索失败不返回 5xx：latest_status=failed + latest_error，前端据此弹三选项。"""
    case_id = _new_case(client, "查新-API失败")
    _patch_tool(monkeypatch, FakeRunTool(raises=subprocess.TimeoutExpired(cmd="x", timeout=180)))

    resp = client.post(f"{API}/cases/{case_id}/search/cnipa", json={"terms": ["API失败词"]})
    assert resp.status_code == 202

    body = _wait_for_search(client, case_id)
    assert body["latest_status"] == "failed"
    assert "超时" in body["latest_error"]
    assert body["count"] == 0


def test_api_manual_entry_and_skip(client: TestClient):
    """REST：人工录入（201）+ 跳过查新（202）。"""
    case_id = _new_case(client, "查新-API人工")
    resp = client.post(
        f"{API}/cases/{case_id}/search/hits",
        json={
            "hits": [
                {
                    "url": "https://patents.google.com/patent/CN102345678A",
                    "pub_no": "CN102345678A",
                    "title": "手工录入的对比文件",
                }
            ],
            "note": "用户粘贴",
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()[0]["manual_entry"] is True

    bad = client.post(f"{API}/cases/{case_id}/search/hits", json={"hits": [{"title": "缺URL"}]})
    assert bad.status_code == 422

    skipped = client.post(f"{API}/cases/{case_id}/search/skip", json={"reason": "时间紧"})
    assert skipped.status_code == 202
    body = client.get(f"{API}/cases/{case_id}/search/hits").json()
    assert body["latest_status"] == "manual_pending"


def test_api_validation_and_404(client: TestClient):
    """契约校验：空词 422、未知案件 404、未知命中 404。"""
    case_id = _new_case(client, "查新-API校验")
    assert client.post(f"{API}/cases/{case_id}/search/cnipa", json={"terms": []}).status_code == 422
    assert client.post(f"{API}/cases/不存在/search/cnipa", json={"terms": ["词"]}).status_code == 404
    assert client.get(f"{API}/cases/不存在/search/hits").status_code == 404
    assert client.patch(f"{API}/search/hits/不存在", json={"selected": True}).status_code == 404
    assert client.patch(f"{API}/search/hits/x", json={}).status_code == 422
