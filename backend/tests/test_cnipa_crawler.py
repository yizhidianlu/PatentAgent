"""国知局爬虫与检索 CLI 的单元测试（**不启动浏览器、不触网**）。

用一个按真实站点行为建模的假页面驱动 `run_epub_session`：

- 首页：防护挑战通过后才出现 #searchStr；类型框 id 为 #fmgb / #fmsq / #xxsq / #wgsq
- 结果页（/Dxb/IndexQuery）：**自带同一个检索表单**，类型框 id 却是 #indexSearchModel_fmgb 等
- 结果页**按类型分页签、一次只显示一类**：勾了多类提交一次，只拿得到第一类

这些行为都是 2026-09-28 对真站点逐帧取证得到的；改版时先重新取证，再改这里。

钉住的事实：
- 第 2 个词起在结果页上直接检索，**首页只访问一次**（早先每词都回首页重过挑战，一词约 35s）；
- 结果页上的类型过滤不会被静默丢掉（它的勾选框 id 与首页不同）；
- 截止时刻到了就停，没做的词如实列为 skipped，做完的保留；
- 单词失败不连累其它词；连续失败才判为被拦截；
- 多类范围（发明 + 实用新型）逐类各提交一次；某一类没检成如实记下，不当整词失败；
- 首页防护始终不过 ⇒ blocked；
- UA 跟真实内核版本走，不带 HeadlessChrome。
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "app" / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import cnipa_epub_crawler as crawler
import cnipa_epub_search as search_cli
from cnipa_epub_parse import EpubSearchHit

HOME_BOXES = ("fmgb", "fmsq", "xxsq", "wgsq")


class FakeElement:
    def __init__(self, page: FakePage, el_id: str) -> None:
        self.page = page
        self.id = el_id

    def get_attribute(self, name: str) -> str | None:
        return self.id if name == "id" else None

    def check(self, force: bool = False) -> None:
        self.page.checked[self.id] = True

    def uncheck(self, force: bool = False) -> None:
        self.page.checked[self.id] = False

    def evaluate(self, _js: str) -> None:  # form.evaluate("el => el.submit()")
        self.page._submit()


class FakePage:
    """按真站点行为建模的页面。url ∈ blank / home / result / broken。"""

    def __init__(
        self,
        *,
        home_ok: bool = True,
        fail_terms: tuple[str, ...] = (),
        fail_types: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self.url = "blank"
        self.home_ok = home_ok
        self.fail_terms = set(fail_terms)
        self.fail_types = set(fail_types)          # {(词, 勾选的类别框)}，例 ("甲", "xxsq")
        self.home_visits = 0
        self.submitted: list[str] = []
        self.submitted_types: list[str] = []       # 每次提交时勾着的类别框（本页的 id，去前缀）
        self.checked: dict[str, bool] = {}
        self._search = ""
        self.waits: list[int] = []
        self.result_timeouts: list[int] = []

    # ---- 导航 ----
    def goto(self, _url: str, **_kw: Any) -> None:
        self.home_visits += 1
        self.url = "home"

    def expect_navigation(self, **_kw: Any) -> Any:
        return contextlib.nullcontext()

    def wait_for_timeout(self, ms: int) -> None:
        self.waits.append(ms)

    # ---- DOM ----
    def query_selector(self, sel: str) -> FakeElement | None:
        has_form = (self.url == "home" and self.home_ok) or self.url == "result"
        if sel in ("#searchStr", "#indexForm"):
            return FakeElement(self, sel[1:]) if has_form else None
        box = sel[1:]
        if self.url == "home" and self.home_ok and box in HOME_BOXES:
            return FakeElement(self, box)
        if self.url == "result" and box.startswith("indexSearchModel_") and box.split("_", 1)[1] in HOME_BOXES:
            return FakeElement(self, box)
        return None

    def fill(self, _sel: str, value: str) -> None:
        self._search = value

    def evaluate(self, _js: str, arg: Any = None) -> None:
        if isinstance(arg, dict) and "id" in arg:  # 勾选兜底
            self.checked[arg["id"]] = bool(arg["checked"])
        else:
            self._submit()

    def _boxes_on(self) -> str:
        """当前页面上勾着的类别框（首页认 #fmgb，结果页认 #indexSearchModel_fmgb）。"""
        prefix = "" if self.url == "home" else "indexSearchModel_"
        return "+".join(b for b in HOME_BOXES if self.checked.get(prefix + b))

    def _submit(self) -> None:
        boxes = self._boxes_on()
        self.submitted.append(self._search)
        self.submitted_types.append(boxes)
        self._shown = boxes
        broken = self._search in self.fail_terms or (self._search, boxes) in self.fail_types
        self.url = "broken" if broken else "result"

    def wait_for_function(self, _js: str, **kw: Any) -> None:
        self.result_timeouts.append(int(kw.get("timeout") or 0))
        if self.url != "result":
            raise TimeoutError("Timeout 120000ms exceeded.")

    def content(self) -> str:
        return f"<html><body>{self._search}|{getattr(self, '_shown', '')}</body></html>"


class FakeBrowser:
    def __init__(self, page: FakePage, version: str = "153.0.8010.53") -> None:
        self.page = page
        self.version = version
        self.contexts: list[dict[str, Any]] = []
        self.closed = False

    def new_context(self, **kw: Any) -> Any:
        self.contexts.append(kw)
        return SimpleNamespace(new_page=lambda: self.page, close=lambda: None)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_site(monkeypatch: pytest.MonkeyPatch):
    """构造一套假站点；返回 (page, browser, factory)。解析函数替换为按词造一条命中。"""

    def make(**page_kw: Any):
        page = FakePage(**page_kw)
        browser = FakeBrowser(page)
        pw = SimpleNamespace(chromium=SimpleNamespace(launch=lambda **_kw: browser))
        return page, browser, (lambda: contextlib.nullcontext(pw))

    monkeypatch.setattr(
        crawler,
        "parse_search_result_html",
        lambda html: [EpubSearchHit(raw_html="", title=html, pub_number=f"CN{abs(hash(html)) % 10**9}A",
                                    link=f"http://epub.cnipa.gov.cn/p/{abs(hash(html)) % 10**9}")],
    )
    monkeypatch.setenv("EPUB_WAF_MAX_WAIT_SEC", "30")
    return make


def test_second_term_onwards_reuses_result_page(fake_site):
    page, _browser, factory = fake_site()
    run = crawler.run_epub_session(["甲", "乙", "丙"], patent_type="invention", playwright_factory=factory)
    assert run.searched == ["甲", "乙", "丙"]
    assert run.stop is None and not run.failed and not run.skipped
    assert page.home_visits == 1, "只该过一次首页防护；每词回首页正是 7 个词要 248s 的原因"
    assert page.submitted == ["甲", "乙", "丙"]
    # 相邻两词之间放慢一点（真站点上请求太密会触发限频）
    assert page.waits.count(int(crawler.TERM_PACING_SEC * 1000)) == 2
    # 单个词等结果页的上限收紧到 45s：一个卡住的词不能再吃掉 120s
    assert page.result_timeouts and max(page.result_timeouts) <= crawler.TERM_STEP_CAP_MS


def test_type_filter_is_applied_on_result_page(fake_site):
    """结果页上的类型框 id 是 indexSearchModel_*——只认首页 id 会静默丢掉过滤。"""
    page, _browser, factory = fake_site()
    crawler.run_epub_session(["甲", "乙"], patent_type="invention", playwright_factory=factory)
    # 第 1 个词在首页勾选
    assert page.checked["fmgb"] is True and page.checked["xxsq"] is False
    # 第 2 个词在结果页上勾选：必须命中结果页的 id
    assert page.checked["indexSearchModel_fmgb"] is True
    assert page.checked["indexSearchModel_xxsq"] is False
    assert page.checked["indexSearchModel_wgsq"] is False


def test_prior_art_scope_submits_each_type_separately(fake_site):
    """结果页一次只显示一类：发明 + 实用新型必须各提交一次，否则实用新型那一页签永远看不到。

    早先勾两类提交一次，拿到的只是「发明公布」；`all` 实际上也只检了发明公布。
    """
    page, _browser, factory = fake_site()
    events: list[dict[str, Any]] = []
    run = crawler.run_epub_session(
        ["甲", "乙"],
        patent_type="invention_utility_model",
        playwright_factory=factory,
        on_event=lambda kind, d: events.append(d) if kind == "term" else None,
    )
    assert run.searched == ["甲", "乙"] and not run.partial
    assert page.submitted == ["甲", "甲", "乙", "乙"]
    assert page.submitted_types == ["fmgb+fmsq", "xxsq", "fmgb+fmsq", "xxsq"]
    assert page.home_visits == 1
    # 两类的命中合并进同一个词
    assert len(run.rows[0][2]) == 2
    assert events[0]["types"] == {"发明": 1, "实用新型": 1} and events[0]["type_errors"] == {}
    # 同一个词两类之间也放慢一点
    assert page.waits.count(int(crawler.TYPE_PACING_SEC * 1000)) == 2


def test_all_types_really_means_all(fake_site):
    page, _browser, factory = fake_site()
    crawler.run_epub_session(["甲"], playwright_factory=factory)          # 缺省 all
    assert page.submitted_types == ["fmgb+fmsq", "xxsq", "wgsq"]


def test_one_type_failing_keeps_the_term(fake_site):
    """实用新型那次提交失败：发明的结果保留，这个词算检过，缺口如实记下；下一个词照常。"""
    page, _browser, factory = fake_site(fail_types=(("甲", "xxsq"),))
    events: list[dict[str, Any]] = []
    run = crawler.run_epub_session(
        ["甲", "乙"],
        patent_type="invention_utility_model",
        playwright_factory=factory,
        on_event=lambda kind, d: events.append(d) if kind == "term" else None,
    )
    assert run.searched == ["甲", "乙"] and not run.failed and run.stop is None
    assert list(run.partial) == ["甲"] and "utility_model" in run.partial["甲"]
    assert events[0]["types"] == {"发明": 1} and list(events[0]["type_errors"]) == ["实用新型"]
    assert page.home_visits == 2                  # 失败把页面带偏，下一个词先回首页


def test_first_type_failing_fails_the_term_without_trying_the_rest(fake_site):
    """第一类就失败：不再接着试后面的类（站点多半在拦截，接着试只会把这个词拖成几倍耗时）。"""
    page, _browser, factory = fake_site(fail_types=(("甲", "fmgb+fmsq"),))
    run = crawler.run_epub_session(
        ["甲", "乙"], patent_type="invention_utility_model", playwright_factory=factory
    )
    assert [t for t, _ in run.failed] == ["甲"] and run.searched == ["乙"]
    assert page.submitted == ["甲", "乙", "乙"]


def test_deadline_between_types_is_reported_not_overrun(fake_site, monkeypatch: pytest.MonkeyPatch):
    import time

    _page, _browser, factory = fake_site()
    monkeypatch.setattr(crawler, "TYPE_MIN_SEC", 10_000.0)   # 第一类之后剩余时间一定不够
    run = crawler.run_epub_session(
        ["甲"], patent_type="invention_utility_model", deadline=time.monotonic() + 60, playwright_factory=factory
    )
    assert run.searched == ["甲"]
    assert run.partial == {"甲": {"utility_model": "时间预算用尽，未检索"}}


def test_deadline_stops_before_next_term_and_keeps_done(fake_site, monkeypatch: pytest.MonkeyPatch):
    import time

    _page, _browser, factory = fake_site()
    monkeypatch.setattr(crawler, "MIN_TERM_SEC", 10_000.0)  # 第 1 个词之后剩余时间一定不够
    run = crawler.run_epub_session(
        ["甲", "乙", "丙"], deadline=time.monotonic() + 60, playwright_factory=factory
    )
    assert run.searched == ["甲"]                 # 第一个词总会做
    assert run.skipped == ["乙", "丙"]
    assert run.stop == "deadline"
    assert "2 个词未检索" in (run.error or "")


def test_single_failure_does_not_sink_the_session(fake_site):
    page, _browser, factory = fake_site(fail_terms=("乙",))
    events: list[str] = []
    run = crawler.run_epub_session(
        ["甲", "乙", "丙"], playwright_factory=factory, on_event=lambda kind, _d: events.append(kind)
    )
    assert run.searched == ["甲", "丙"]
    assert [t for t, _ in run.failed] == ["乙"]
    assert run.stop is None
    assert page.home_visits == 2                  # 失败把页面带偏后回首页恢复
    assert events.count("term") == 2 and events.count("term_failed") == 1


def test_consecutive_failures_mean_blocked(fake_site):
    _page, _browser, factory = fake_site(fail_terms=("甲", "乙"))
    run = crawler.run_epub_session(["甲", "乙", "丙"], playwright_factory=factory)
    assert run.searched == []
    assert [t for t, _ in run.failed] == ["甲", "乙"]
    assert run.skipped == ["丙"]
    assert run.stop == "blocked"


def test_home_gate_never_passing_is_blocked(fake_site):
    _page, browser, factory = fake_site(home_ok=False)
    waits: list[dict[str, Any]] = []
    run = crawler.run_epub_session(
        ["甲", "乙"],
        playwright_factory=factory,
        on_event=lambda kind, d: waits.append(d) if kind == "wait" else None,
    )
    assert run.stop == "blocked"
    assert run.rows == [] and run.skipped == ["甲", "乙"]
    assert "#searchStr" in (run.error or "")
    assert waits, "等防护挑战期间要有心跳，否则界面会判成卡死"
    assert browser.closed is True


def test_navigation_race_during_challenge_is_not_blocked(fake_site):
    """挑战页跑完 JS 会自己跳转；查询撞在跳转途中会抛错——那是「还没好」，不是被拦截。

    真站点上实测撞到过：我把「先等 3 秒再查」改成「先立刻查一次」后，这个竞态就暴露了。
    """
    from playwright.sync_api import Error as PlaywrightError

    page, _browser, factory = fake_site()
    original = page.query_selector
    raised = {"n": 0}

    def racy(sel: str):
        if sel == "#searchStr" and raised["n"] < 2:
            raised["n"] += 1
            raise PlaywrightError(
                "Page.query_selector: Execution context was destroyed, most likely because of a navigation"
            )
        return original(sel)

    page.query_selector = racy  # type: ignore[method-assign]
    run = crawler.run_epub_session(["甲"], playwright_factory=factory)
    assert raised["n"] == 2
    assert run.stop is None and run.searched == ["甲"]


def test_result_page_ready_waits_for_the_whole_document():
    """导航只等到 commit：结果页还在流式到达时第一个 div.item 就已出现，那一刻读到的是半截页面。

    真站点上一页 3 条只解析到 1 条（2026-09-28 取证）。就绪条件必须包含「文档已解析完」。
    """
    assert 'document.readyState === "loading"' in crawler._RESULT_PAGE_READY_JS


def test_user_agent_follows_real_browser_version(fake_site):
    _page, browser, factory = fake_site()
    crawler.run_epub_session(["甲"], playwright_factory=factory)
    ua = browser.contexts[0]["user_agent"]
    assert "Chrome/153.0.0.0" in ua
    assert "Headless" not in ua
    assert "Chrome/120" not in ua


def test_desktop_user_agent_fallbacks():
    assert crawler.desktop_user_agent(None) == crawler.DEFAULT_USER_AGENT
    assert "Edg/153.0.0.0" in crawler.desktop_user_agent("153.1.2.3", "msedge")


def test_timeout_ms_is_bounded_by_deadline():
    import time

    assert crawler._timeout_ms(None, 120_000) == 120_000
    near = crawler._timeout_ms(time.monotonic() + 30, 120_000)
    assert 25_000 < near <= 30_000
    # 预算已尽：给下限让这一步快速失败，而不是 0 或负数
    assert crawler._timeout_ms(time.monotonic() - 10, 120_000) == 5_000


def test_compat_search_keywords_is_all_or_nothing(fake_site):
    """旧接口保持全有或全无的语义（给不关心部分结果的旧调用方）。"""
    _page, _b, factory = fake_site(fail_terms=("乙",))
    with pytest.raises(RuntimeError, match="仅完成 2/3"):
        crawler.search_epub_keywords(["甲", "乙", "丙"], playwright_factory=factory)
    _page, _b, factory = fake_site(home_ok=False)
    with pytest.raises(TimeoutError):
        crawler.search_epub_keywords(["甲"], playwright_factory=factory)


# ---------------------------------------------------------------------------
# CLI 协议（cnipa_epub_search.main）
# ---------------------------------------------------------------------------


def _hit(n: int) -> EpubSearchHit:
    return EpubSearchHit(raw_html="<x/>", title=f"标题{n}", pub_number=f"CN{n}A", link=f"http://e/{n}")


def _lines(out: str, prefix: str) -> list[Any]:
    return [json.loads(line[len(prefix):]) for line in out.splitlines() if line.startswith(prefix)]


def test_cli_partial_run_reports_every_term_and_summary(monkeypatch: pytest.MonkeyPatch, capsys):
    seen: dict[str, Any] = {}

    def fake_run(terms, *, patent_type, deadline, on_event, **_kw):
        seen["deadline"] = deadline
        on_event("home_ready", {"sec": 8.6})
        on_event("term", {"i": 1, "n": 3, "term": "甲", "sec": 2.0, "hits": [_hit(1)]})
        return crawler.EpubRun(
            rows=[("甲", "<html/>", [_hit(1)])], skipped=["乙", "丙"], stop="deadline",
            error="时间预算用尽，剩余 2 个词未检索",
        )

    monkeypatch.setattr(crawler, "run_epub_session", fake_run)
    monkeypatch.setenv("EPUB_DEADLINE_SEC", "120")
    rc = search_cli.main(["--type", "all", "甲", "乙", "丙"])
    out = capsys.readouterr().out

    assert rc == 0                                   # 部分完成也是成功退出
    assert seen["deadline"] is not None              # 预算作为截止时刻传进去
    term = _lines(out, "EPUB_TERM_JSON:")[0]
    assert term["term"] == "甲" and term["hits"][0]["pub_number"] == "CN1A"
    assert "raw_html" not in term["hits"][0]
    summary = _lines(out, "EPUB_SUMMARY_JSON:")[0]
    assert summary == {"searched": ["甲"], "failed": [], "skipped": ["乙", "丙"], "stop": "deadline",
                       "error": "时间预算用尽，剩余 2 个词未检索"}
    assert len(_lines(out, "EPUB_HITS_JSON:")[0]) == 1
    # 最终结果行在最后，旧消费方只认它也不会读错
    assert out.strip().splitlines()[-1].startswith("EPUB_HITS_JSON:")


def test_cli_blocked_exits_3_without_result_line(monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.setattr(
        crawler,
        "run_epub_session",
        lambda terms, **_kw: crawler.EpubRun(skipped=list(terms), stop="blocked", error="首页未出现检索框"),
    )
    rc = search_cli.main(["甲"])
    captured = capsys.readouterr()
    assert rc == 3
    assert _lines(captured.out, "EPUB_SUMMARY_JSON:")[0]["stop"] == "blocked"
    assert not _lines(captured.out, "EPUB_HITS_JSON:")
    assert "首页未出现检索框" in captured.err


# ---------------------------------------------------------------------------
# 结果页解析（按 2026-09-28 真站点「公布模式」结果页裁剪）
# ---------------------------------------------------------------------------

_ITEM = """<div class="item"> <div class="title"><h1 class="title">[实用新型] 一种纤维支气管镜训练箱</h1></div>
<div class="info"> <dl><dt>授权公告号：</dt><dd>CN205451563U</dd></dl> <dl><dt>授权公告日：</dt><dd>2016.08.10</dd></dl>
<dl> <dt> 申请人： </dt> <dd> 奥林巴斯医疗株式会社; <a href="javascript:;" class="open j-open-allinfo">全部</a>
<div class="allinfo"> 国立研究开发法人国立癌症研究中心</div> </dd> </dl> </div>
<div class="intro"> <dl> <dt> 摘要： </dt> <dd class="chopping"><p>本实用新型公开了一种纤维支气管镜训练箱，隔<i class="point">...</i><span class="alltxt" style="display:none">板四与箱体的底板之间设置有十六孔分隔块。</span><a href="javascript:" class="open j-open-alltxt">全部</a></p></dd></dl></div>
</div>"""


def test_parse_joins_folded_abstract_without_ellipsis():
    """站点把 200 字以后折进隐藏 span，并在断点插「...」和「全部」：摘要里不能留下这些。

    早先解析出的摘要中间凭空多出「 ... 」，还把断点处的词劈开（「隔 ... 板四」）。
    """
    from cnipa_epub_parse import parse_search_result_html

    html = f'<div id="result"><div class="overview-default">{_ITEM}</div></div>'
    [hit] = parse_search_result_html(html)
    assert hit.abstract == "本实用新型公开了一种纤维支气管镜训练箱，隔板四与箱体的底板之间设置有十六孔分隔块。"
    assert hit.pub_number == "CN205451563U"
    assert hit.pub_date == "2016-08-10"
    assert hit.applicant == "奥林巴斯医疗株式会社; 国立研究开发法人国立癌症研究中心"   # 折起来的第二申请人也在
