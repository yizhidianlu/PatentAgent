# -*- coding: utf-8 -*-
"""
中国专利公布公告网站点：http://epub.cnipa.gov.cn/ —— **首页「公布公告查询」** 检索（#indexForm / #searchStr）。

须安装 **Playwright**。浏览器启动见 ``tools/shared/browser.py``（系统 Chrome → Edge → 自带 Chromium；有系统浏览器时不必 ``playwright install chromium``）。若只需内存中解析、不落盘 HTML，优先用同目录 **`cnipa_epub_search.py`**；
本文件侧重 **写出结果页 HTML** 与可插拔的 ``fetch_epub_result_html`` API。

-------------------------------------------------------------------------------
一、整体流程（一场多词检索）
-------------------------------------------------------------------------------
1. 启动浏览器（默认无头；系统 Chrome → Edge → 自带 Chromium；可用环境变量改为有界面）。
2. 新建浏览器上下文：设定 **与真实内核同版本的桌面 UA**、**zh-CN**、固定 **视口**（见 ``_new_context``）。
3. ``page.goto`` 站点首页，**wait_until="load"**。
4. **等待首页可检索**：首页在访客到达后会先经 **前端脚本/WAF 一类逻辑**（实测为瑞数动态防护：先回 202 挑战页，JS 跑完后才给真页面），未通过前 **不会出现** 检索输入框 ``#searchStr``。本实现 **周期性轮询 DOM**（每 3 秒一次，上限见 ``EPUB_WAF_MAX_WAIT_SEC``）直到 ``#searchStr`` 出现；**不是**用 requests 直接 POST 能等价替代的步骤。
5. ``page.fill`` 将关键词写入 ``#searchStr``，对 ``#indexForm`` 执行 **submit**，并等待结果页导航 **commit**。
6. 等待结果页就绪：标题为 **「专利查询结果展示」或「无查询结果」**（见 ``EPUB_TITLE_*`` 常量），且 ``#result`` 内出现列表条目（``div.item`` / ``h1.title``）或明确零结果文案；不等待完整 ``load``。国知局改版时需同步调整常量与 ``_RESULT_PAGE_READY_JS``。
7. ``page.content()`` 取全页 HTML；若处于导航中抛错则 **重试退避**（``_safe_page_content``），避免竞态。
8. **第 2 个词起直接在结果页上检索**：结果页 ``/Dxb/IndexQuery`` 自带同一个 ``#indexForm`` / ``#searchStr``
   （带新的防伪 token，类型勾选沿用上次提交的状态），所以不必回首页、不必再过一遍防护挑战。
   早先每个词都回首页重过挑战，一词约 35 秒，7 个词 248 秒——而外层预算只有 180 秒。
9. 解析由 **`cnipa_epub_parse.py`** 完成。

-------------------------------------------------------------------------------
一·补、时间预算与部分结果（``run_epub_session``）
-------------------------------------------------------------------------------
- 调用方给一个绝对截止时刻 ``deadline``（``time.monotonic()`` 口径）。每一步的超时都取
  「该步上限」与「剩余预算」的较小者，保证**脚本总能在外层杀进程之前自己收尾并报出原因**。
- 开始下一个词之前若剩余不足 ``MIN_TERM_SEC``，就停下来，把没来得及检索的词如实列为 skipped。
- **已完成的词永远保留**。早先超时是全有或全无：7 个词完成了 5 个，一超时 5 个的结果一起作废。
- 单个词失败不连累其它词（回首页重来）；**连续** ``MAX_CONSECUTIVE_FAILS`` 个词失败才判为被拦截并停止，
  免得在已被封的状态下把剩余预算全耗在注定失败的请求上。
- 首页门控（第一次过防护）失败 ⇒ ``stop="blocked"``：这是唯一真正意义上的「被拦截/不可达」。

-------------------------------------------------------------------------------
二、策略摘要：在解决什么、用了哪些手段
-------------------------------------------------------------------------------
- **为何用 Playwright**：站点依赖 **浏览器内 JavaScript** 渲染与风控后再开放检索框；**纯 HTTP 抓取**往往拿不到含 ``#searchStr`` 的可用首页或拿不到真实结果 DOM。
- **所谓「绕过」**：指 **技术层面** 与无头自动化、静态抓取之间的 gap——通过 **真实 Chromium 内核 + 等待 JS 完成 + 常见浏览器指纹**（UA、语言、viewport）降低「一进来就_submit」的失败率；**不**表示规避法律法规或站点服务条款，用途应限合法检索与交底书查新辅助。
- **反自动化/特征**：启动参数 ``--disable-blink-features=AutomationControlled`` 用于减弱 Chromium 的 **webdriver 自动化开关** 暴露（效果因站点升级而变，非保证）。
- **不覆盖的场景**：图形/滑块验证码、短信验证、强制登录等——若站点突然启用，本脚本**无**专门破解逻辑；可尝试 ``PLAYWRIGHT_HEADED=1`` 人工辅助或改用 **WebSearch**（见 ``prompts/prior_art_search.md``）。

-------------------------------------------------------------------------------
三、检索关键词建议
-------------------------------------------------------------------------------
- 公布站首页检索框对 **多个词** 通常按 **同时包含（AND）** 理解，**词多且专**时极易 **0 条**；**建议每次尽量使用单个词或极短短语** 做一次检索，需要宽召回时可用 **`cnipa_epub_search.py`**（按空白拆成多词、多次检索再合并），或分多次手动换关键词。
- 本脚本命令行默认仍接受一个参数字符串（可含空格）；含空格时与浏览器内一次提交一致，语义上仍是 **整句 AND**，不等同于拆词多查。

-------------------------------------------------------------------------------
环境变量
-------------------------------------------------------------------------------
  EPUB_WAF_MAX_WAIT_SEC  轮询等待 #searchStr 的最长时间，默认 180（另受 deadline 约束）
  PLAYWRIGHT_HEADED        设为 1 时使用有界面 Chromium
  EPUB_RESULT_HTML         结果页 HTML 完整路径；不设则 tools/_last_result_YYYYMMDDHHmmss.html
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from playwright.sync_api import Browser, BrowserContext, Error, Page, Playwright, sync_playwright

_TOOLS_DIR = Path(__file__).resolve().parents[1]
_SHARED = _TOOLS_DIR / "shared"
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from cnipa_epub_parse import EpubSearchHit, hits_to_jsonable, parse_search_result_html
from browser import launch_chromium
from stdio_utf8 import ensure_utf8_stdio
from patent_type import (
    EPUB_CHECKBOX_IDS,
    TYPE_ALL,
    TYPE_DESIGN,
    TYPE_INVENTION,
    TYPE_LABEL_ZH,
    TYPE_UTILITY_MODEL,
    epub_checkbox_states,
    epub_query_types,
    normalize_patent_type,
)


EPUB_BASE = "http://epub.cnipa.gov.cn/"
# 国知局 /Dxb/IndexQuery 结果页 <title>；改版时须同步单测与 _RESULT_PAGE_READY_JS
EPUB_TITLE_RESULT = "专利查询结果展示"
EPUB_TITLE_NO_HIT = "无查询结果"
# 在浏览器内判断结果页可解析：文档解析完 + title + #result DOM（列表或零结果文案）。
# 「解析完」不能省：导航只等到 commit，结果页还在流式到达时第一个 div.item 就已出现，
# 那一刻读出的是半截页面——真站点上一页 3 条只解析到 1 条（2026-09-28 取证）。
_RESULT_PAGE_READY_JS = """(titles) => {
    if (document.readyState === "loading") return false;
    const t = document.title.trim();
    if (t === titles.noHit) return true;
    if (t !== titles.result) return false;
    const r = document.querySelector("#result");
    if (!r) return false;
    if (r.querySelector("div.item, h1.title")) return true;
    const html = r.innerHTML;
    if (
        html.includes("无查询结果") ||
        html.includes("没有找到") ||
        html.includes("未检索到") ||
        html.includes("0条")
    ) {
        return true;
    }
    return false;
}"""
# 结果页交互（2026-09-28 真站点取证）：
# - 结果页按类型分页签（发明公布 / 发明授权 / 实用新型 / 外观设计），一次只显示一个页签；
# - 默认每页 3 条，按公布公告日倒序；只有页面自己的「每页 10 条」下拉框能改，而站点只认
#   可信的用户事件——脚本派发的 change 事件被忽略，键盘在下拉框上按 ↓ 则会带出请求；
# - 切页签、翻页、改每页条数都是页内 AJAX（/Dxb/PageQuery），只替换 #result 的内容，
#   点当前页签不会重新查询；请求里的 pageSize 取自隐藏域，改成 10 之后切页签、翻页都沿用。
_MARK_JS = """() => {
    const r = document.getElementById('result');
    if (r) r.insertAdjacentHTML('afterbegin', '<i id="__epub_stale__"></i>');
}"""
# 等到 #result 被替换、且（给了类型时）里面的条目确实是这一类：条目标题带「[实用新型]」
# 这样的前缀。只看「被替换了」不够——上一次操作的响应可能迟到，恰好在切到下一个页签后
# 才回来，把页面刷回上一类的内容；那时读到的全是重复条目，一类就被误记成 0 条
# （真实案件里「支气管镜训练箱」的三篇实用新型就这样丢了）。
_FRESH_JS = """(label) => {
    const r = document.getElementById('result');
    if (!r || document.getElementById('__epub_stale__')) return false;
    if (!label) return true;
    const titles = [...r.querySelectorAll('div.item h1.title')].map(h => h.textContent.trim());
    return titles.length > 0 && titles.every(t => !t.startsWith('[') || t.startsWith('[' + label + ']'));
}"""
_PAGES_RE = re.compile(r"共\s*(\d+)\s*页")
# 页签文字 → 类型。发明授权（B 文本）不读：内容与其发明公布（A 文本）相同，读了只会重复。
_TAB_TYPES = {"发明公布": TYPE_INVENTION, "实用新型": TYPE_UTILITY_MODEL, "外观设计": TYPE_DESIGN}
# 每页条数：站点只给 3 / 10 两档
PAGE_SIZE = 10
# 每个词每类最多翻到第几页（10 条一页）。宽泛的词命中成百上千条、按日期倒序，
# 再往后翻拿到的也只是更多的近期无关文献；窄的词一两页就见底。
MAX_PAGES_PER_TYPE = 2

# 仅在拿不到真实内核版本时兜底用；正常路径见 desktop_user_agent()
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 结果页上的类型勾选框 id 与首页不同（首页 #fmgb，结果页 #indexSearchModel_fmgb）
_RESULT_PAGE_BOX_PREFIX = "indexSearchModel_"

# 开始下一个词至少要剩这么多秒：结果页直接提交 + 等结果 + 解析，实测 2~8s，
# 回首页重过挑战约 10~20s。留足余量，宁可少检索一个词，也不要被外层强杀。
MIN_TERM_SEC = 20.0
# 连续这么多个词失败，判为已被拦截，停止烧剩余预算
MAX_CONSECUTIVE_FAILS = 2
# 单个词「提交 → 结果页就绪」每一步的上限。正常 2~15s；早先沿用 120s，
# 真站点上一个卡住的词就烧掉了 137s，把后面所有词的预算吃光。
TERM_STEP_CAP_MS = 45_000
# 相邻两个词之间的间隔。结果页复用后请求变得很密（不再每词回首页），真站点上
# 第 3 个词起开始卡——与此前「第 4~5 个词后耗时陡增」的观察一致，像是触发了限频。
# 对政府站点放慢一点本来也是应有的礼貌。
TERM_PACING_SEC = 4.0
# 同一个词在结果页上相邻两次页内操作（切页签 / 翻页 / 改每页条数）的间隔。
# 页内 AJAX 比整页导航轻得多，间隔可以比换词短一些。
AJAX_PACING_SEC = 1.5
# 再做一次页内操作至少要剩这么多秒（实测一次 1~3s）
AJAX_MIN_SEC = 8.0
# 一次页内操作等 #result 被替换的上限。站点慢的时候一次要十几秒；等不到就按当前页面读
AJAX_CAP_MS = 30_000
# 等待防护挑战期间，每隔这么多秒报一次「还在等」
WAIT_BEAT_SEC = 15.0


class EpubBlockedError(TimeoutError):
    """首页始终没有出现检索框：访问验证未通过或页面加载不了（真正意义上的「被拦截/不可达」）。

    继承 TimeoutError 以兼容旧调用方的 except 分支。
    """


def _max_wait_sec() -> float:
    return float(os.environ.get("EPUB_WAF_MAX_WAIT_SEC", "180"))


def _timeout_ms(deadline: float | None, cap_ms: int, *, floor_ms: int = 5_000) -> int:
    """单步超时 = min(该步上限, 剩余预算)。

    预算已尽时仍给一个下限，让这一步**快速失败**而不是无限等——
    外层早先的问题正是「goto 120s + 等挑战 160s > 子进程上限 180s」，
    脚本还没来得及说清卡在哪，就被整个杀掉了。
    """
    if deadline is None:
        return cap_ms
    left_ms = int((deadline - time.monotonic()) * 1000)
    return max(floor_ms, min(cap_ms, left_ms))


def _short(exc: BaseException, limit: int = 200) -> str:
    """异常 → 一行可读原因（Playwright 的报错常带多行调用日志）。"""
    text = str(exc).strip().splitlines()
    head = text[0] if text else type(exc).__name__
    return head[:limit]


def _headed() -> bool:
    return os.environ.get("PLAYWRIGHT_HEADED", "").strip() in ("1", "true", "yes")


def default_result_html_path() -> Path:
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    return Path(__file__).resolve().parent / f"_last_result_{ts}.html"


def wait_for_epub_home_ready(
    page: Page,
    *,
    max_wait_sec: float | None = None,
    deadline: float | None = None,
    on_wait: Callable[[float], None] | None = None,
) -> None:
    """打开首页并等到检索框出现（即防护挑战已通过）。

    - ``deadline``：绝对截止时刻；goto 超时与轮询上限都不会越过它。
    - ``on_wait(sec)``：每等 ``WAIT_BEAT_SEC`` 秒回调一次——等挑战可能要十几秒，
      这段时间里必须有东西证明「还在等」，否则界面会把它判成卡死。
    """
    limit = max_wait_sec if max_wait_sec is not None else _max_wait_sec()
    if deadline is not None:
        limit = min(limit, max(5.0, deadline - time.monotonic()))
    try:
        page.goto(EPUB_BASE, wait_until="load", timeout=_timeout_ms(deadline, 120_000))
    except Error as exc:
        raise EpubBlockedError(f"国知局首页打不开：{_short(exc)}") from exc
    elapsed = 0.0
    step = 3.0
    next_beat = WAIT_BEAT_SEC
    while True:
        if _has_search_box(page):
            return
        if elapsed >= limit:
            break
        page.wait_for_timeout(int(step * 1000))
        elapsed += step
        if on_wait is not None and elapsed >= next_beat:
            on_wait(elapsed)
            next_beat += WAIT_BEAT_SEC
    raise EpubBlockedError(
        f"{limit:.0f}s 内首页未出现检索框 #searchStr（访问验证未通过或页面未加载完）；"
        "可增大 EPUB_WAF_MAX_WAIT_SEC 或设置 PLAYWRIGHT_HEADED=1"
    )


def _has_search_box(page: Page) -> bool:
    """检索框出现了吗。**页面正在跳转时查询会抛错，那只说明「还没好」，不是失败。**

    瑞数挑战页跑完 JS 会自己跳到真页面；查询恰好撞在跳转途中时，Playwright 报
    「Execution context was destroyed, most likely because of a navigation」。
    把它当致命错误，就会把一次正常通过的挑战误判成「被拦截」——真站点上实测撞到过。
    """
    try:
        return page.query_selector("#searchStr") is not None
    except Error:
        return False


def _can_search_here(page: Page) -> bool:
    """当前页能否直接发起下一次检索（结果页自带同一个检索表单）。"""
    try:
        return bool(page.query_selector("#searchStr") and page.query_selector("#indexForm"))
    except Error:
        return False


def _safe_page_content(page: Page, *, max_attempts: int = 10) -> str:
    last_err: Exception | None = None
    for i in range(max_attempts):
        try:
            return page.content()
        except Error as e:
            msg = str(e).lower()
            last_err = e
            if "navigating" not in msg and "changing" not in msg:
                raise
            try:
                page.wait_for_load_state("load", timeout=20_000)
            except Exception:
                pass
            page.wait_for_timeout(400 + 200 * i)
    if last_err:
        raise last_err
    raise RuntimeError("_safe_page_content: 未返回内容")


def _wait_result_page_ready(
    page: Page, *, deadline: float | None = None, cap_ms: int = 120_000
) -> None:
    """等结果页 title 与 #result 列表/零结果 DOM 就绪（不等完整 load）。"""
    page.wait_for_function(
        _RESULT_PAGE_READY_JS,
        arg={"result": EPUB_TITLE_RESULT, "noHit": EPUB_TITLE_NO_HIT},
        timeout=_timeout_ms(deadline, cap_ms),
    )


def _type_box(page: Page, cid: str) -> Any:
    """按 id 找类型勾选框：首页叫 #fmgb，结果页叫 #indexSearchModel_fmgb。

    两处都要认——在结果页上直接发起下一次检索时若只认首页 id，
    会**静默**找不到元素、跳过勾选，类型过滤就这么丢了且不报错。
    """
    return page.query_selector(f"#{cid}") or page.query_selector(f"#{_RESULT_PAGE_BOX_PREFIX}{cid}")


def wanted_boxes(query_types: tuple[str, ...]) -> dict[str, bool]:
    """一次提交要勾选的类型框：各类的并集，但不勾发明授权（见 _TAB_TYPES）。"""
    states = dict.fromkeys(EPUB_CHECKBOX_IDS, False)
    for ptype in query_types:
        for cid, want in epub_checkbox_states(ptype).items():
            states[cid] = states[cid] or want
    states["fmsq"] = False
    return states


def apply_epub_type_filter(
    page: Page, patent_type: str = TYPE_ALL, *, boxes: dict[str, bool] | None = None
) -> None:
    """按类型勾选 发明公布/发明授权/实用新型/外观设计 四类（首页与结果页通用）。"""
    states = boxes if boxes is not None else epub_checkbox_states(patent_type)
    for cid, want in states.items():
        box = _type_box(page, cid)
        if not box:
            continue
        box_id = box.get_attribute("id") or cid
        try:
            if want:
                box.check(force=True)
            else:
                box.uncheck(force=True)
        except Error:
            page.evaluate(
                """({id, checked}) => {
                    const el = document.getElementById(id);
                    if (!el) return;
                    el.checked = checked;
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new Event('click', { bubbles: true }));
                }""",
                {"id": box_id, "checked": want},
            )


def submit_index_search(
    page: Page,
    keyword: str,
    *,
    patent_type: str = TYPE_ALL,
    boxes: dict[str, bool] | None = None,
    deadline: float | None = None,
    cap_ms: int = 120_000,
) -> None:
    """在当前页（首页或结果页）的 #indexForm 上提交一次检索并等结果页就绪。"""
    apply_epub_type_filter(page, patent_type, boxes=boxes)
    page.fill("#searchStr", keyword)
    with page.expect_navigation(timeout=_timeout_ms(deadline, cap_ms), wait_until="commit"):
        form = page.query_selector("#indexForm")
        if form:
            form.evaluate("el => el.submit()")
        else:
            page.evaluate(
                """() => {
                const f = document.getElementById('indexForm');
                if (f) f.submit();
            }"""
            )
    _wait_result_page_ready(page, deadline=deadline, cap_ms=cap_ms)


def fetch_epub_result_html(
    keyword: str,
    *,
    patent_type: str = TYPE_ALL,
    playwright_factory: Callable[[], Playwright] | None = None,
) -> str:
    """
    只拉取检索结果页 HTML，不在此函数内做正文解析。
    解析请使用 ``cnipa_epub_parse.parse_search_result_html(html)``。
    """
    rows = search_epub_keywords(
        [keyword], patent_type=patent_type, playwright_factory=playwright_factory
    )
    return rows[0][0]


EventCallback = Callable[[str, dict[str, Any]], None]


@dataclass
class EpubRun:
    """一场多词检索的结果：**完成的、失败的、没来得及的，各自如实记录**。"""

    rows: list[tuple[str, str, list[EpubSearchHit]]] = field(default_factory=list)  # (词, html, 命中)
    failed: list[tuple[str, str]] = field(default_factory=list)                     # (词, 原因)
    # 词做完了、但其中某一类没检成（例：发明检完，实用新型那次提交失败）：{词: {类型: 原因}}
    partial: dict[str, dict[str, str]] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)                                # 预算用尽未开始
    stop: str | None = None      # None=全部跑完 | "deadline"=预算用尽 | "blocked"=被拦截/不可达
    error: str | None = None     # stop 的原因说明

    @property
    def searched(self) -> list[str]:
        return [term for term, _html, _hits in self.rows]


def _ajax(page: Page, action: Callable[[], Any], *, deadline: float | None, label: str | None = None) -> None:
    """做一次页内操作（切页签 / 翻页 / 改每页条数）并等到 #result 被站点替换。

    先往 #result 里塞一个标记元素：站点的 AJAX 回来会整个换掉 #result，标记随之消失。
    不能只等「内容变了」——每页 3 条切到 10 条时，命中不足 3 条的页签内容一个字都不变。
    """
    page.evaluate(_MARK_JS)
    action()
    page.wait_for_function(_FRESH_JS, arg=label, timeout=_timeout_ms(deadline, AJAX_CAP_MS))


def _raise_page_size(page: Page, *, deadline: float | None, label: str | None = None) -> bool:
    """把每页条数从 3 切到 10（顺带刷新当前页签）。切不过去返回 False，由调用方读默认的 3 条。"""
    if not page.query_selector("#sizeSelect"):
        return False
    if page.input_value("#sizeSelect") == str(PAGE_SIZE):
        return True
    try:
        # 下拉框只有 3 / 10 两项：焦点在框上按一次 ↓ 就是 10；键盘事件是可信事件
        _ajax(
            page,
            lambda: (page.focus("#sizeSelect"), page.keyboard.press("ArrowDown")),
            deadline=deadline,
            label=label,
        )
    except Exception:  # noqa: BLE001 —— 切不过去不算失败，只是少拿几条
        return False
    return True


def _tabs(page: Page) -> list[tuple[str, Any, bool]]:
    """结果页左侧的类型页签：``(文字, 元素, 是否当前)``。"""
    out: list[tuple[str, Any, bool]] = []
    for el in page.query_selector_all(".j-select-item a"):
        label = (el.inner_text() or "").strip()
        current = "curr" in (el.get_attribute("class") or "")
        out.append((label, el, current))
    return out


def _total_pages(page: Page) -> int:
    el = page.query_selector(".page_total")
    m = _PAGES_RE.search(el.inner_text() or "") if el else None
    return int(m.group(1)) if m else 1


def _next_page(page: Page, *, deadline: float | None, label: str | None = None) -> bool:
    btn = page.query_selector(".next_page")
    if not btn or "btn_dis" in (btn.get_attribute("class") or ""):
        return False
    _ajax(page, btn.click, deadline=deadline, label=label)
    return True


def _search_term(
    page: Page,
    term: str,
    query_types: tuple[str, ...],
    *,
    deadline: float | None,
    reserve_sec: float = 0.0,
) -> tuple[str, list[EpubSearchHit], dict[str, int], dict[str, str]]:
    """一个词：提交一次，把要读的类型页签逐个读全（每页 10 条、最多 MAX_PAGES_PER_TYPE 页）。

    返回 ``(最后一页 html, 命中, {类型: 条数}, {类型: 失败原因})``。``reserve_sec`` 是要给
    后面的词留的时间：页内翻页只在预算宽裕时做，宁可少翻一页，也要让每个词都检到。
    某一类失败就不再试后面的类：失败多半意味着页面被带偏或站点在限流，接着试只会把
    这个词拖成几倍的耗时。**一类都没检成才算这个词失败**（抛出，交给外层计入连续失败）。
    """
    if not _can_search_here(page):
        # 上一个词把页面带偏了（失败页 / 无结果页都没有检索表单）：回首页，通常已有挑战 cookie
        wait_for_epub_home_ready(page, deadline=deadline)
    submit_index_search(
        page, term, boxes=wanted_boxes(query_types), deadline=deadline, cap_ms=TERM_STEP_CAP_MS
    )
    html = _safe_page_content(page)
    if not page.query_selector("#sizeSelect"):
        # 「无查询结果」页：所有类型都没有命中，页上也没有页签和下拉框
        return html, [], dict.fromkeys(query_types, 0), {}

    merged: list[EpubSearchHit] = []
    seen: set[str] = set()
    counts: dict[str, int] = {}
    errors: dict[str, str] = {}

    def take(ptype: str, hits: list[EpubSearchHit]) -> None:
        # 只数新条目：翻回一个页签时第一页会再出现一次
        fresh = 0
        for h in hits:
            key = h.pub_number or h.link or (h.title or "")[:120]
            if key not in seen:
                seen.add(key)
                merged.append(h)
                fresh += 1
        counts[ptype] = counts.get(ptype, 0) + fresh

    def budget_ok() -> bool:
        return deadline is None or deadline - time.monotonic() >= reserve_sec + AJAX_MIN_SEC

    def pace() -> None:
        page.wait_for_timeout(int(AJAX_PACING_SEC * 1000))

    wanted = {label: t for label, t in _TAB_TYPES.items() if t in query_types}
    tabs = [(label, el, cur) for label, el, cur in _tabs(page) if label in wanted]
    tabs.sort(key=lambda x: not x[2])           # 当前页签先读：切每页条数会顺带刷新它
    # 站点只给有命中的类型出页签：勾了却没有页签的类型就是 0 条，不是「没检成」
    shown = {label for label, _el, _cur in tabs}
    for label, ptype in wanted.items():
        if label not in shown:
            counts[ptype] = 0
    # 结果页「可解析」不等于「脚本装完」：页面脚本还没把下拉框、页签的处理绑上就去按，
    # 站点要过十几秒才响应（真站点上第一页因此只读到 3 条）。等 load 完，再留一点间隔。
    try:
        page.wait_for_load_state("load", timeout=_timeout_ms(deadline, AJAX_CAP_MS))
    except Exception:  # noqa: BLE001 —— 个别外链资源挂着不算数，靠间隔兜底
        pass

    # 第一轮：每个页签读第一页。第二页价值远低于另一类的第一页——预算紧时先保住每一类的
    # 第一页（真实案件里曾因先翻发明第 2 页，把「纤维支气管镜训练箱」的三篇实用新型挤掉）。
    on_tab: str | None = None
    more: list[str] = []
    read_ok: set[str] = set()               # 真正读到了页面的类型
    broken = False
    for j, (label, el, current) in enumerate(tabs):
        ptype = wanted[label]
        if j > 0 and not budget_ok():
            errors[ptype] = "时间预算用尽，未检索"
            continue
        try:
            pace()
            if current:
                _raise_page_size(page, deadline=deadline, label=label)
            else:
                _ajax(page, el.click, deadline=deadline, label=label)
            on_tab = label
            html = _safe_page_content(page)
            take(ptype, parse_search_result_html(html))
            read_ok.add(ptype)
            if _total_pages(page) > 1:
                more.append(label)
        except Exception as exc:  # noqa: BLE001 —— 记下是哪一类，由调用方决定整词成败
            errors[ptype] = _short(exc)
            broken = True
            break

    # 第二轮：还有下一页的页签按需翻页（当前所在的页签先翻，省一次切换）。
    # 翻页失败不算缺口：这一类的第一页已经在手。
    more.sort(key=lambda lb: lb != on_tab)
    for label in [] if broken else more:
        if not budget_ok():
            break
        try:
            if label != on_tab:
                el = next((e for lb, e, _c in _tabs(page) if lb == label), None)
                if el is None:
                    continue
                pace()
                _ajax(page, el.click, deadline=deadline, label=label)
                on_tab = label
                take(wanted[label], parse_search_result_html(_safe_page_content(page)))
            for _n in range(2, min(_total_pages(page), MAX_PAGES_PER_TYPE) + 1):
                if not budget_ok():
                    break
                pace()
                if not _next_page(page, deadline=deadline, label=label):
                    break
                html = _safe_page_content(page)
                take(wanted[label], parse_search_result_html(html))
        except Exception:  # noqa: BLE001
            break

    if errors and not read_ok:
        raise RuntimeError(next(iter(errors.values())))
    # 停下之后没轮到的类也要如实记下
    for ptype in query_types:
        if ptype not in counts and ptype not in errors:
            errors[ptype] = "未检索"
    return html, merged, counts, errors


def run_epub_session(
    terms: list[str],
    *,
    patent_type: str = TYPE_ALL,
    deadline: float | None = None,
    on_event: EventCallback | None = None,
    playwright_factory: Callable[[], Playwright] | None = None,
) -> EpubRun:
    """一场检索共用一个浏览器、**只过一次防护挑战**；按截止时刻收口，保留已完成的词。

    ``on_event(kind, data)``，kind ∈ ``wait`` / ``home_ready`` / ``term`` / ``term_failed``，
    供 CLI 逐行上报进度——外层据此在界面上显示「已完成 3/7」，也据此证明检索还活着。
    只有浏览器起不来这类环境错误才会抛出；被拦截、超预算都体现在返回值里。
    """
    run = EpubRun()
    if not terms:
        return run
    emit = on_event or (lambda _kind, _data: None)
    query_types = epub_query_types(patent_type)
    pw_gen = playwright_factory or sync_playwright
    with pw_gen() as p:
        browser, label = _launch_browser_labeled(p)
        context = _new_context(browser, label)
        try:
            page = context.new_page()
            t0 = time.monotonic()
            try:
                wait_for_epub_home_ready(
                    page, deadline=deadline, on_wait=lambda sec: emit("wait", {"stage": "home", "sec": int(sec)})
                )
            except Exception as exc:  # noqa: BLE001 —— 首页门控失败 = 被拦截/不可达
                run.stop, run.error = "blocked", _short(exc)
                run.skipped = list(terms)
                return run
            emit("home_ready", {"sec": round(time.monotonic() - t0, 1)})

            fails_in_row = 0
            total = len(terms)
            for i, term in enumerate(terms):
                if i > 0 and deadline is not None and deadline - time.monotonic() < MIN_TERM_SEC:
                    run.stop = "deadline"
                    run.error = f"时间预算用尽，剩余 {total - i} 个词未检索"
                    run.skipped = list(terms[i:])
                    break
                if i > 0:
                    page.wait_for_timeout(int(TERM_PACING_SEC * 1000))
                started = time.monotonic()
                try:
                    html, hits, counts, type_errors = _search_term(
                        page, term, query_types, deadline=deadline, reserve_sec=(total - i - 1) * MIN_TERM_SEC
                    )
                except Exception as exc:  # noqa: BLE001 —— 单词失败不连累其它词
                    fails_in_row += 1
                    reason = _short(exc)
                    run.failed.append((term, reason))
                    emit("term_failed", {"i": i + 1, "n": total, "term": term, "error": reason})
                    if fails_in_row >= MAX_CONSECUTIVE_FAILS:
                        run.stop = "blocked"
                        run.error = f"连续 {fails_in_row} 个词检索失败，判为已被拦截：{reason}"
                        run.skipped = list(terms[i + 1 :])
                        break
                    continue
                fails_in_row = 0
                run.rows.append((term, html, hits))
                if type_errors:
                    run.partial[term] = type_errors
                emit(
                    "term",
                    {
                        "i": i + 1,
                        "n": total,
                        "term": term,
                        "sec": round(time.monotonic() - started, 1),
                        "hits": hits,
                        "types": {TYPE_LABEL_ZH.get(t, t): n for t, n in counts.items()},
                        "type_errors": {TYPE_LABEL_ZH.get(t, t): e for t, e in type_errors.items()},
                    },
                )
            return run
        finally:
            context.close()
            browser.close()


def search_epub_keywords(
    terms: list[str],
    *,
    patent_type: str = TYPE_ALL,
    playwright_factory: Callable[[], Playwright] | None = None,
) -> list[tuple[str, list[EpubSearchHit]]]:
    """兼容旧接口：全部成功才返回（与 ``terms`` 等长的 ``(html, hits)``），否则抛出。

    新代码请用 ``run_epub_session``——它会保留部分结果，而不是全有或全无。
    """
    run = run_epub_session(terms, patent_type=patent_type, playwright_factory=playwright_factory)
    if run.stop == "blocked" and not run.rows:
        raise EpubBlockedError(run.error or "国知局访问验证未通过")
    if run.failed or run.skipped:
        done = len(run.rows)
        detail = run.error or (run.failed[0][1] if run.failed else "")
        raise RuntimeError(f"仅完成 {done}/{len(terms)} 个检索词：{detail}")
    return [(html, hits) for _term, html, hits in run.rows]


def search_epub_keyword(
    keyword: str,
    *,
    patent_type: str = TYPE_ALL,
    playwright_factory: Callable[[], Playwright] | None = None,
) -> tuple[str, list[EpubSearchHit]]:
    rows = search_epub_keywords(
        [keyword], patent_type=patent_type, playwright_factory=playwright_factory
    )
    return rows[0]


def search_epub_keyword_with_page(
    page: Page,
    keyword: str,
    *,
    patent_type: str = TYPE_ALL,
) -> tuple[str, list[EpubSearchHit]]:
    wait_for_epub_home_ready(page)
    submit_index_search(page, keyword, patent_type=patent_type)
    html = _safe_page_content(page)
    return html, parse_search_result_html(html)


def _launch_browser_labeled(p: Playwright) -> tuple[Browser, str]:
    return launch_chromium(p, headless=not _headed())


def _launch_browser(p: Playwright) -> Browser:
    browser, _label = _launch_browser_labeled(p)
    return browser


def _os_token() -> str:
    """UA 里的平台段与真实平台一致（navigator.platform 骗不了，UA 就别自相矛盾）。"""
    if sys.platform.startswith("win"):
        return "Windows NT 10.0; Win64; x64"
    if sys.platform == "darwin":
        return "Macintosh; Intel Mac OS X 10_15_7"
    return "X11; Linux x86_64"


def desktop_user_agent(browser_version: str | None, label: str = "chrome") -> str:
    """按**真实内核版本**拼桌面 UA（采用 Chrome 的 UA 精简格式：只保留主版本号）。

    无头模式的默认 UA 带 ``HeadlessChrome``，所以必须覆盖；但覆盖成一个写死的旧版本
    （早先是 Chrome/120，而本机内核是 153）会让「UA 声称的版本」与「页面能用的新 API」
    对不上，这是功能检测层面的自动化特征。版本号跟着真实内核走，就不存在这种矛盾。
    """
    major = str(browser_version or "").split(".", 1)[0]
    if not major.isdigit():
        return DEFAULT_USER_AGENT
    ua = (
        f"Mozilla/5.0 ({_os_token()}) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )
    if label == "msedge":
        ua += f" Edg/{major}.0.0.0"
    return ua


def _new_context(browser: Browser, label: str = "chrome") -> BrowserContext:
    try:
        version = browser.version
    except Exception:  # noqa: BLE001 —— 取不到版本就用兜底 UA，不因此放弃检索
        version = None
    return browser.new_context(
        user_agent=desktop_user_agent(version, label),
        locale="zh-CN",
        viewport={"width": 1280, "height": 900},
    )


def _dump_home_debug() -> None:
    """调试：仅拉取首页并保存 WAF 通过后 HTML。"""
    out = Path(__file__).resolve().parent / "_last_home.html"
    with sync_playwright() as p:
        browser = _launch_browser(p)
        context = _new_context(browser)
        page = context.new_page()
        try:
            wait_for_epub_home_ready(page)
            out.write_text(page.content(), encoding="utf-8")
            print("已保存:", out)
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    ensure_utf8_stdio()
    argv = [a for a in sys.argv[1:] if a.strip()]
    if argv and argv[0] in ("--dump-home", "-d"):
        _dump_home_debug()
        sys.exit(0)
    patent_type = TYPE_ALL
    filtered: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--type", "-t") and i + 1 < len(argv):
            patent_type = normalize_patent_type(argv[i + 1], default=TYPE_ALL)
            i += 2
            continue
        if a.startswith("--type="):
            patent_type = normalize_patent_type(a.split("=", 1)[1], default=TYPE_ALL)
            i += 1
            continue
        filtered.append(a)
        i += 1
    kw = (filtered[0] if filtered else "批处理").strip()
    try:
        out_html, hits = search_epub_keyword(kw, patent_type=patent_type)
    except Exception as e:
        print("CNIPA_EPUB_ERROR:", e, file=sys.stderr)
        sys.exit(1)
    out_path = Path(
        os.environ.get("EPUB_RESULT_HTML", "").strip() or default_result_html_path()
    )
    out_path = out_path.expanduser().resolve()
    out_path.write_text(out_html, encoding="utf-8")
    print(
        "结果页长度",
        len(out_html),
        "解析条目数",
        len(hits),
        file=sys.stderr,
        flush=True,
    )
    print("结果页 HTML 已保存:", out_path, file=sys.stderr, flush=True)
    print(
        "EPUB_HITS_JSON:",
        json.dumps(hits_to_jsonable(hits), ensure_ascii=False),
        flush=True,
    )
