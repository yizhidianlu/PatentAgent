"""CNIPA 查新服务与 API 测试（prompt-porting-spec §2 A4 / R8）。

**不依赖真实网络**：把 `app.services.cnipa._stream_script` 换成脚本化的假子进程输出，按
`tools/cnipa_epub_search.py` 的真实 stdout 协议（逐词行 + 摘要行 + `EPUB_HITS_JSON:` 结果行）
喂数据，其余环节（解析 → 分类 → 落库 → 缓存 → 降级 → 人工兜底 → REST 契约）全部真跑。
conftest 另有一道保险：没替换就去拉起真实检索脚本的用例，会得到一次「无法启动」而不是触网。

`_stream_script` 自身（逐行读、按时强杀、强杀后抢救已打出的行）用**本地的小 python 脚本**
真跑子进程来测，同样不触网。

覆盖：
- 解析入库（URL 照抄 link；无 link 条目丢弃）；
- 6 小时缓存命中（同案件复用旧命中；跨案件复制一份）；部分完成的会话不进缓存；
- 时间预算随词数伸缩，并作为截止时刻传给脚本；
- **部分结果保留**：预算用尽 / 被强杀时，已完成的词照常入库，没做的词如实列出；
- 失败分类说真话：被拦截（blocked）/ 超预算（budget）/ 零命中（empty）/ 脚本错误（script）；
- 逐词进度推到界面，且刷新流水线的「卡住」计时；
- 人工兜底录入、勾选、跳过查新、URL 白名单；
- 浏览器探测；
- REST：POST/GET/PATCH 五个端点。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Sequence
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
    """浏览器探测仍走阻塞的 run_tool。"""
    monkeypatch.setattr(cnipa, "run_tool", fake)
    return fake


# ---- 检索子进程：按脚本真实协议拼输出行 ----


def _line(prefix: str, payload: Any) -> str:
    return f"{prefix} {json.dumps(payload, ensure_ascii=False)}"


def _home_ready(sec: float = 8.6) -> str:
    return _line("EPUB_HOME_READY:", {"sec": sec})


def _term(i: int, n: int, term: str, hits: list[dict[str, Any]], sec: float = 2.0) -> str:
    return _line("EPUB_TERM_JSON:", {"i": i, "n": n, "term": term, "sec": sec, "hits": hits})


def _summary(
    searched: Sequence[str],
    skipped: Sequence[str] = (),
    failed: Sequence[str] = (),
    stop: str | None = None,
    error: str | None = None,
) -> str:
    return _line(
        "EPUB_SUMMARY_JSON:",
        {"searched": list(searched), "failed": list(failed), "skipped": list(skipped), "stop": stop, "error": error},
    )


def _hits_line(hits: list[dict[str, Any]]) -> str:
    return _line("EPUB_HITS_JSON:", hits)


def _success(terms: Sequence[str], hits: list[dict[str, Any]]) -> list[str]:
    """一场全部完成的检索输出：命中都算在第一个词上。"""
    lines = [_home_ready()]
    for i, t in enumerate(terms, 1):
        lines.append(_term(i, len(terms), t, hits if i == 1 else []))
    lines += [_summary(terms), _hits_line(hits)]
    return lines


class FakeStream:
    """替换 `cnipa._stream_script`：记录调用，把预置行逐条喂给 on_line，按预置返回。"""

    def __init__(
        self,
        lines: Sequence[str] = (),
        *,
        rc: int | None = 0,
        killed: bool = False,
        raises: BaseException | None = None,
    ) -> None:
        self.lines = list(lines)
        self.rc = rc
        self.killed = killed
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    def __call__(self, args: list[str], env: dict[str, str], hard_limit: float, on_line: Any) -> Any:
        self.calls.append({"args": list(args), "env": dict(env), "hard_limit": hard_limit})
        if self.raises is not None:
            raise self.raises
        for line in self.lines:
            if on_line is not None:
                on_line(line)
        return list(self.lines), self.rc, self.killed


def _patch_stream(monkeypatch: pytest.MonkeyPatch, fake: FakeStream) -> FakeStream:
    monkeypatch.setattr(cnipa, "_stream_script", fake)
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


def test_prior_art_scope_covers_both_technical_types():
    """现有技术不分专利类型：实用新型案件也要检发明公布（反之亦然）；外观设计仍只检外观。

    2026-09-28 真实案件：实用新型案件只勾了「实用新型」，用户自己在 Google 上找到的两篇
    最相关文献（CN101656028A、CN114186982A）都是发明公布——平台从一开始就不可能检到。
    """
    assert cnipa.prior_art_scope("utility_model") == "invention_utility_model"
    assert cnipa.prior_art_scope("invention") == "invention_utility_model"
    assert cnipa.prior_art_scope(None) == "invention_utility_model"
    assert cnipa.prior_art_scope("design") == "design"
    assert cnipa.normalize_type("invention_utility_model") == "invention_utility_model"


def test_rank_hits_puts_title_matches_first_and_is_stable():
    hits = [
        {"title": "某公司", "abstract": "", "url": "u1"},                         # 只在别的字段撞上
        {"title": "训练箱", "abstract": "", "url": "u2"},
        {"title": "x", "abstract": "一种支气管镜训练箱", "url": "u3"},
        {"title": "支气管镜训练箱", "abstract": "支气管镜", "url": "u4"},
        {"title": "某医院", "abstract": "", "url": "u5"},
    ]
    ranked = cnipa.rank_hits(hits, ["训练箱", "支气管镜"])
    # u4 标题两词 4 分；u2 标题一词、u3 摘要两词都是 2 分，同分保持原顺序；其余 0 分
    assert [h["url"] for h in ranked] == ["u4", "u2", "u3", "u1", "u5"]


def test_progress_message_shows_per_type_counts():
    line = _line(
        "EPUB_TERM_JSON:",
        {"i": 2, "n": 7, "term": "软镜训练", "sec": 5.2, "hits": [{}, {}, {}],
         "types": {"发明": 2, "实用新型": 1}, "type_errors": {}},
    )
    assert cnipa._progress_message(line) == "已完成 2/7：「软镜训练」发明 2 条、实用新型 1 条（5.2s）"
    gap = _line(
        "EPUB_TERM_JSON:",
        {"i": 1, "n": 7, "term": "训练箱", "sec": 3.0, "hits": [{}],
         "types": {"发明": 1}, "type_errors": {"实用新型": "Timeout"}},
    )
    assert cnipa._progress_message(gap) == "已完成 1/7：「训练箱」发明 1 条（3.0s）；实用新型未检成"


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
    fake = _patch_stream(monkeypatch, FakeStream(_success(["资源画像", "任务调度"], HITS_PAYLOAD)))

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

    # 子进程参数：--type 映射 + 一次会话传多词；预算按词数算，作为截止时刻传给脚本
    call = fake.calls[0]
    assert call["args"] == ["--type", "invention", "资源画像", "任务调度"]
    budget = cnipa.search_budget(2)
    assert call["env"]["EPUB_DEADLINE_SEC"] == str(budget)
    assert "EPUB_WAF_MAX_WAIT_SEC" in call["env"]
    # 强杀线 = 预算 + 收尾宽限：脚本总有机会自己收尾、报出原因
    assert call["hard_limit"] == budget + cnipa.TEARDOWN_GRACE_SEC
    assert result.searched_terms == ["资源画像", "任务调度"]
    assert result.partial is False

    # 进度回调覆盖关键阶段；运行中逐词上报（crawl）
    kinds = [s for s, _ in stages]
    assert kinds[:2] == ["start", "running"]
    assert kinds[-2:] == ["parsed", "done"]
    assert "crawl" in kinds
    assert any("已完成 1/2" in m for s, m in stages if s == "crawl")

    # 落库
    queries = await cnipa.list_queries(case_id)
    assert len(queries) == 1
    assert queries[0].status == "done"
    assert queries[0].source == "cnipa"
    assert queries[0].terms == ["资源画像", "任务调度"]
    assert queries[0].hit_count == 2
    hits = await cnipa.list_hits(case_id)
    assert len(hits) == 2


async def test_search_ranks_and_caps_hits(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """逐类检索后命中可达四五十条：按与检索词的重合度排序，只入库前 MAX_SEARCH_HITS 条。"""
    case_id = _new_case(client, "查新-上限")
    noise = [
        {"title": f"某某科技有限公司的无关方案{i}", "pub_number": f"CN1{i:08d}A",
         "link": f"http://epub.cnipa.gov.cn/patent/CN1{i:08d}A", "abstract": "无关"}
        for i in range(cnipa.MAX_SEARCH_HITS + 5)
    ]
    relevant = {"title": "一种软镜训练箱", "pub_number": "CN999999999U",
                "link": "http://epub.cnipa.gov.cn/patent/CN999999999U", "abstract": "软镜训练"}
    _patch_stream(monkeypatch, FakeStream(_success(["软镜训练", "训练箱"], [*noise, relevant])))
    stages: list[tuple[str, str]] = []

    async def on_progress(stage: str, msg: str) -> None:
        stages.append((stage, msg))

    result = await cnipa.search(case_id, ["软镜训练", "训练箱"], "invention_utility_model", on_progress=on_progress)
    assert len(result.hits) == cnipa.MAX_SEARCH_HITS
    assert result.hits[0].pub_no == "CN999999999U"          # 最相关的排到最前，不会被上限截掉
    parsed = next(m for st, m in stages if st == "parsed")
    assert f"解析到 {len(noise) + 1} 条命中" in parsed and f"保留前 {cnipa.MAX_SEARCH_HITS} 条" in parsed
    starting = next(m for st, m in stages if st == "start")
    assert "范围：发明+实用新型" in starting


async def test_search_type_gap_is_reported_and_not_cached(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """某个词的实用新型那次提交没成：结果照收，缺口写进完成提示；这场不进缓存（下次要重检）。"""
    case_id = _new_case(client, "查新-类型缺口")
    lines = [
        _home_ready(),
        _line("EPUB_TERM_JSON:", {"i": 1, "n": 2, "term": "缺口甲", "sec": 2.0, "hits": HITS_PAYLOAD[:1],
                                  "types": {"发明": 1}, "type_errors": {"实用新型": "Timeout 45000ms"}}),
        _line("EPUB_TERM_JSON:", {"i": 2, "n": 2, "term": "缺口乙", "sec": 2.0, "hits": [],
                                  "types": {"发明": 0, "实用新型": 0}, "type_errors": {}}),
        _summary(["缺口甲", "缺口乙"]),
        _hits_line(HITS_PAYLOAD[:1]),
    ]
    fake = _patch_stream(monkeypatch, FakeStream(lines))
    stages: list[tuple[str, str]] = []

    async def on_progress(stage: str, msg: str) -> None:
        stages.append((stage, msg))

    result = await cnipa.search(case_id, ["缺口甲", "缺口乙"], "invention_utility_model", on_progress=on_progress)
    assert result.status == "done" and len(result.hits) == 1
    assert result.gap_terms == ["缺口甲"]                     # 交给流水线自动补检
    done = next(m for st, m in stages if st == "done")
    assert "「缺口甲」的实用新型未检成" in done
    await cnipa.search(case_id, ["缺口甲", "缺口乙"], "invention_utility_model")
    assert len(fake.calls) == 2                               # 有缺口的一场不当完整结果复用


async def test_search_reuses_cache_in_same_case(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """同案件 6 小时内同 terms+type：直接复用，不再起子进程。"""
    case_id = _new_case(client, "查新-缓存同案")
    fake = _patch_stream(monkeypatch, FakeStream(_success(["缓存词甲", "缓存词乙"], HITS_PAYLOAD)))

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
    fake = _patch_stream(monkeypatch, FakeStream(_success(["跨案词甲", "跨案词乙"], HITS_PAYLOAD)))

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
    fake = _patch_stream(monkeypatch, FakeStream(_success(["禁缓存词"], HITS_PAYLOAD)))

    await cnipa.search(case_id, ["禁缓存词"], "invention")
    await cnipa.search(case_id, ["禁缓存词"], "invention", use_cache=False)
    assert len(fake.calls) == 2


async def test_search_killed_before_home_is_blocked(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """被强杀且首页防护始终没过：判「被拦截」——这才是真正的访问验证失败。"""
    case_id = _new_case(client, "查新-超时")
    _patch_stream(monkeypatch, FakeStream([], rc=None, killed=True))

    stages: list[str] = []
    result = await cnipa.search(
        case_id, ["超时词"], "invention", on_progress=lambda stage, msg: stages.append(stage)
    )

    assert result.status == "failed"
    assert result.ok is False
    assert result.failure_kind == "blocked"
    assert "超时" in result.error and "访问验证" in result.error
    assert result.hits == []
    assert stages[-1] == "failed"

    queries = await cnipa.list_queries(case_id)
    assert queries[0].status == "failed"
    assert "超时" in queries[0].error
    assert await cnipa.list_hits(case_id) == []


async def test_search_killed_after_home_is_budget_not_waf(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """首页已通过、却在预算内一个词都没做完：是时间预算问题，**不许**报成拦截。

    早先这两种情况共用「疑似 WAF 拦截或网络不可达」——而真实那次站点正常、检索正在成功进行。
    """
    case_id = _new_case(client, "查新-超预算")
    _patch_stream(monkeypatch, FakeStream([_home_ready()], rc=None, killed=True))
    result = await cnipa.search(case_id, ["预算词甲", "预算词乙"], "invention")
    assert result.status == "failed"
    assert result.failure_kind == "budget"
    assert "站点正常" in result.error
    assert "WAF" not in result.error and "拦截" not in result.error


async def test_search_blocked_exit_code(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """脚本自己判定首页防护未通过（退出码 3）：blocked，原因原样带出。"""
    case_id = _new_case(client, "查新-被拦截")
    lines = [_summary([], skipped=["拦截词"], stop="blocked", error="90s 内首页未出现检索框 #searchStr")]
    _patch_stream(monkeypatch, FakeStream(lines, rc=3))
    result = await cnipa.search(case_id, ["拦截词"], "invention")
    assert result.status == "failed"
    assert result.failure_kind == "blocked"
    assert "访问验证未通过" in result.error
    assert "首页未出现检索框" in result.error
    assert result.skipped_terms == ["拦截词"]


async def test_search_nonzero_exit_degrades(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """脚本非零退出（playwright 缺失等）：failed + 关键行入 error，归为脚本错误。"""
    case_id = _new_case(client, "查新-退出码")
    _patch_stream(
        monkeypatch,
        FakeStream(["ERROR: pip install playwright", "HINT: browser.py --probe"], rc=1),
    )
    result = await cnipa.search(case_id, ["退出码词"], "invention")
    assert result.status == "failed"
    assert result.failure_kind == "script"
    assert "退出码 1" in result.error
    assert "playwright" in result.error


async def test_search_missing_marker_degrades(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """退出码 0 但没有任何机读行（页面改版等）：仍判 failed。"""
    case_id = _new_case(client, "查新-无标记")
    _patch_stream(monkeypatch, FakeStream(["不是机读协议的输出", "EPUB_NOTE: html_bytes=512"], rc=0))
    result = await cnipa.search(case_id, ["无标记词"], "invention")
    assert result.status == "failed"
    assert "EPUB_HITS_JSON" in result.error


async def test_search_zero_hits_degrades(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """全部检索完、确实零命中：判 failed 交人工兜底（A4 禁止编造检索结果），并说清原词重试没用。"""
    case_id = _new_case(client, "查新-零命中")
    _patch_stream(monkeypatch, FakeStream([_home_ready(), _summary(["零命中词"]), _hits_line([])]))
    result = await cnipa.search(case_id, ["零命中词"], "invention")
    assert result.status == "failed"
    assert result.failure_kind == "empty"
    assert "均无命中" in result.error
    assert "原词重试" in result.error
    assert result.searched_terms == ["零命中词"]
    queries = await cnipa.list_queries(case_id)
    assert queries[0].status == "failed"

    # empty_is_failure=False 时按 done 处理（供上层按需放宽）
    case2 = _new_case(client, "查新-零命中放宽")
    _patch_stream(monkeypatch, FakeStream([_home_ready(), _summary(["零命中词2"]), _hits_line([])]))
    result2 = await cnipa.search(case2, ["零命中词2"], "invention", empty_is_failure=False)
    assert result2.status == "done"
    assert result2.hits == []


async def test_search_empty_terms(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """空检索词：直接 failed，不起子进程也不写库。"""
    case_id = _new_case(client, "查新-空词")
    fake = _patch_stream(monkeypatch, FakeStream(_success(["x"], HITS_PAYLOAD)))
    result = await cnipa.search(case_id, ["  "], "invention")
    assert result.status == "failed"
    assert result.error == "检索词为空"
    assert fake.calls == []
    assert await cnipa.list_queries(case_id) == []


# ---------------------------------------------------------------------------
# 时间预算与部分结果
# ---------------------------------------------------------------------------


def test_search_budget_scales_with_terms():
    """预算随词数伸缩、封顶——早先一个 180s 写死套在任意词数上，7 个词实测要 248s。"""
    assert cnipa.search_budget(1) < cnipa.search_budget(4) < cnipa.search_budget(7)
    assert cnipa.search_budget(7) > 180
    # 每词要读两个类型页签、每页 10 条、按需翻页：6 个词至少要给到 5 分钟
    #（真实案件里 210s 只做完 4 个词，还有两个词的实用新型页签因预算没读）
    assert cnipa.search_budget(6) >= 300
    assert cnipa.search_budget(100) == cnipa.SEARCH_BUDGET_CAP
    assert cnipa.search_budget(0) == cnipa.search_budget(1)


async def test_search_partial_keeps_completed_terms(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """预算用尽：已完成词的命中照常入库，没来得及的词如实列出；**不许进缓存**。"""
    case_id = _new_case(client, "查新-部分完成")
    terms = ["部分甲", "部分乙", "部分丙"]
    lines = [
        _home_ready(),
        _term(1, 3, "部分甲", HITS_PAYLOAD[:1]),
        _term(2, 3, "部分乙", HITS_PAYLOAD[1:2]),
        _summary(["部分甲", "部分乙"], skipped=["部分丙"], stop="deadline", error="时间预算用尽，剩余 1 个词未检索"),
        _hits_line(HITS_PAYLOAD[:2]),
    ]
    fake = _patch_stream(monkeypatch, FakeStream(lines))
    stages: list[tuple[str, str]] = []
    result = await cnipa.search(case_id, terms, "invention", on_progress=lambda s, m: stages.append((s, m)))

    assert result.status == "done"
    assert len(result.hits) == 2
    assert result.searched_terms == ["部分甲", "部分乙"]
    assert result.skipped_terms == ["部分丙"]
    assert result.partial is True
    assert result.pending_terms == ["部分丙"]
    done_msg = next(m for s, m in stages if s == "done")
    assert "部分丙" in done_msg and "未检索" in done_msg

    # 同一组词再搜：部分结果不能当完整结果复用，必须真跑
    await cnipa.search(case_id, terms, "invention")
    assert len(fake.calls) == 2


async def test_search_salvages_completed_terms_when_killed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """脚本被强杀、没来得及打摘要：从逐词行里抢救已完成的词，而不是全部作废。"""
    case_id = _new_case(client, "查新-强杀抢救")
    lines = [_home_ready(), _term(1, 3, "抢救甲", HITS_PAYLOAD[:2])]
    _patch_stream(monkeypatch, FakeStream(lines, rc=None, killed=True))
    result = await cnipa.search(case_id, ["抢救甲", "抢救乙", "抢救丙"], "invention")
    assert result.status == "done"
    assert [h.url for h in result.hits] == [
        "http://epub.cnipa.gov.cn/patent/CN114567890A",
        "http://epub.cnipa.gov.cn/patent/CN113456789B",
    ]
    assert result.searched_terms == ["抢救甲"]
    assert result.skipped_terms == ["抢救乙", "抢救丙"]


async def test_search_zero_hits_but_unfinished_is_budget(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """做完的词零命中、但还有词没做：是预算问题，不是「确实查不到」。"""
    case_id = _new_case(client, "查新-零命中未做完")
    lines = [_home_ready(), _summary(["未完甲"], skipped=["未完乙"], stop="deadline"), _hits_line([])]
    _patch_stream(monkeypatch, FakeStream(lines))
    result = await cnipa.search(case_id, ["未完甲", "未完乙"], "invention")
    assert result.status == "failed"
    assert result.failure_kind == "budget"
    assert result.pending_terms == ["未完乙"]
    assert "未完乙" in result.error


async def test_search_spawn_failure_never_raises(client: TestClient):
    """子进程起不来（conftest 的保险就是这么模拟的）：降级为 script 失败，绝不抛。"""
    case_id = _new_case(client, "查新-起不来")
    result = await cnipa.search(case_id, ["起不来"], "invention")
    assert result.status == "failed"
    assert result.failure_kind == "script"
    assert "无法启动检索脚本" in result.error


def test_hub_progress_touches_stall_timer(monkeypatch: pytest.MonkeyPatch):
    """每条检索进度都要刷新流水线的「卡住」计时——国知局子进程不在 LLM 在途登记里。"""
    import asyncio

    from app.services import progress as progress_service
    from app.services.sse import hub

    touched: list[tuple[str, str]] = []
    monkeypatch.setattr(progress_service, "touch", lambda cid, detail="": touched.append((cid, detail)))

    async def fake_emit(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(hub, "emit", fake_emit)
    cb = cnipa.hub_progress("case-touch", step_key="prior_art_search")
    asyncio.run(cb("crawl", "已完成 2/7"))
    assert touched == [("case-touch", "已完成 2/7")]


def test_parse_search_protocol_tolerates_noise():
    """坏行、未知行不拖垮解析；逐词行出现即说明首页已通过。"""
    parsed = cnipa.parse_search_protocol(
        [
            "BROWSER: channel=chrome",
            "EPUB_TERM_JSON: {坏掉的JSON",
            _term(1, 1, "甲", HITS_PAYLOAD[:1]),
            "EPUB_MERGE: terms=1",
        ]
    )
    assert parsed["home_ready"] is True
    assert [t["term"] for t in parsed["terms"]] == ["甲"]
    assert parsed["summary"] is None and parsed["hits"] is None
    assert "BROWSER: channel=chrome" in parsed["notes"]


# ---------------------------------------------------------------------------
# 流式子进程：本地小脚本真跑（不触网）
# ---------------------------------------------------------------------------


def _local_script(tmp_path, body: str):
    """写一个本地 python 脚本，并返回一个替身 spawn_tool：拉起它而不是真实检索脚本。"""
    script = tmp_path / "fake_search.py"
    script.write_text(body, encoding="utf-8")

    def spawn(_name: str, args: list[str], *, extra_env: dict[str, str] | None = None) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-u", str(script), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

    return spawn


def test_stream_script_reads_lines_and_exit_code(tmp_path, monkeypatch: pytest.MonkeyPatch):
    body = (
        "import sys\n"
        "print('EPUB_HOME_READY: {\"sec\": 1}', flush=True)\n"
        "print('EPUB_NOTE: hello', file=sys.stderr, flush=True)\n"
        "print('EPUB_HITS_JSON: []', flush=True)\n"
        "sys.exit(0)\n"
    )
    monkeypatch.setattr(cnipa, "spawn_tool", _local_script(tmp_path, body))
    seen: list[str] = []
    lines, rc, killed = cnipa._stream_script([], {}, 30, seen.append)
    assert rc == 0 and killed is False
    assert "EPUB_HITS_JSON: []" in lines
    assert "EPUB_NOTE: hello" in lines          # stderr 合并进来，没有被丢
    assert seen == lines                        # 逐行实时回调


def test_stream_script_kills_on_overrun_and_keeps_printed_lines(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """子进程卡住不出声：按时强杀；强杀前已经打出的行（已完成的词）要留下来。"""
    body = (
        "import time\n"
        "print('EPUB_TERM_JSON: {\"i\": 1, \"n\": 3, \"term\": \"甲\", \"hits\": []}', flush=True)\n"
        "time.sleep(60)\n"
    )
    monkeypatch.setattr(cnipa, "spawn_tool", _local_script(tmp_path, body))
    started = time.monotonic()
    lines, _rc, killed = cnipa._stream_script([], {}, 2, None)
    assert killed is True
    assert time.monotonic() - started < 20            # 没被卡在阻塞的 readline 上
    assert any(line.startswith("EPUB_TERM_JSON:") for line in lines)


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
    _patch_stream(monkeypatch, FakeStream(_success(["API词甲", "API词乙"], HITS_PAYLOAD)))

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


def test_api_search_defaults_to_prior_art_scope(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """不指定类型时按查新口径检发明 + 实用新型，而不是只检案件自己那一类。"""
    case_id = _new_case(client, "查新-API缺省范围")
    fake = _patch_stream(monkeypatch, FakeStream(_success(["范围词甲", "范围词乙"], HITS_PAYLOAD)))
    resp = client.post(f"{API}/cases/{case_id}/search/cnipa", json={"terms": ["范围词甲", "范围词乙"]})
    assert resp.status_code == 202, resp.text
    assert resp.json()["patent_type"] == "invention_utility_model"
    _wait_for_search(client, case_id)
    assert fake.calls[0]["args"][:2] == ["--type", "invention_utility_model"]


def test_api_search_failure_is_reported_not_500(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """检索失败不返回 5xx：latest_status=failed + latest_error，前端据此弹三选项。"""
    case_id = _new_case(client, "查新-API失败")
    _patch_stream(monkeypatch, FakeStream([], rc=None, killed=True))

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
