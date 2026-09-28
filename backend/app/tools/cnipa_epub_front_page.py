# -*- coding: utf-8 -*-
"""国知局公布公告：按公开号取单行本的扉页图（交给 OCR 认摘要）。

老专利的公开文本是扫描件：PDF 没有文字层，公布公告的检索结果里也没有摘要，Google Patents
详情页又常拿不到（限流、出口 IP 被标记）。这是最后一条能拿到扉页文字的路：结果条目上的
「发明专利申请 / 实用新型专利」按钮会打开单行本阅读器（egaz.cnipa.gov.cn），阅读器按页给图，
第 1 页就是扉页（2026-09-28 真站点取证：``showPage?path=…&page=1`` 回 JSON，图在 ``/imgs/`` 下）。

用法：
  python cnipa_epub_front_page.py --out 目录 CN101656028A [CN…]

输出（stdout，一行一条）：
  EPUB_WAIT / EPUB_HOME_READY            同检索脚本（等防护挑战的心跳）
  EPUB_PAGE_JSON: {"pub", "path", "pages", "title", "applicant", "pub_date", "abstract", "link", "sec"}
  EPUB_PAGE_FAIL_JSON: {"pub", "error"}
  EPUB_PAGES_DONE: <取到的篇数>
环境变量 ``EPUB_DEADLINE_SEC``：整场的时间预算（秒）。
退出码：0 跑完（个别失败写在行里）；2 参数错误；3 首页防护没过，一篇都没开始。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import cnipa_epub_crawler as crawler
from cnipa_epub_parse import EpubSearchHit, parse_search_result_html
from patent_type import TYPE_ALL, epub_query_types, infer_patent_type_from_pub
from stdio_utf8 import ensure_utf8_stdio

# 单行本阅读器：等弹窗、等 iframe、取图，每一步的上限
VIEWER_CAP_MS = 45_000
# 开始下一篇至少要剩这么多秒（检索 + 弹窗 + 取图实测 6~10s）
MIN_PUB_SEC = 15.0
IMAGE_MAGIC = (b"\x89PNG", b"\xff\xd8\xff")

EventCallback = Callable[[str, dict[str, Any]], None]


def _digits(pub: str | None) -> str:
    return re.sub(r"\D", "", pub or "")


def _find_item(page: Any, digits: str) -> tuple[Any, EpubSearchHit] | None:
    """当前页签里号码匹配的条目：``(条目元素, 解析出的著录)``。"""
    hits = parse_search_result_html(crawler._safe_page_content(page))
    hit = next((h for h in hits if _digits(h.pub_number) == digits), None)
    if hit is None:
        return None
    for item in page.query_selector_all("#result div.item"):
        if re.search(rf"CN\s*{digits}", item.inner_text() or ""):
            return item, hit
    return None


# 公开号数字部分小于这个数的（约 2013 年以前公布），站上的号码没有种类字母：CN101656028
_OLD_NUMBER_BELOW = 103_000_000


def _query_order(pub: str) -> list[str]:
    """先用哪种写法查：老号码先纯数字、新号码先原样；查不到再换另一种（多花一次「回首页」十几秒）。"""
    digits = _digits(pub)
    try:
        old = 0 < int(digits) < _OLD_NUMBER_BELOW
    except ValueError:
        old = False
    order = [digits, pub] if old else [pub, digits]
    return [q for q in dict.fromkeys(order) if q]


def _search_pub(page: Any, pub: str, *, deadline: float | None) -> tuple[Any, EpubSearchHit] | None:
    """按公开号检索并定位条目。老文献在站上没有种类字母（CN101656028），带字母查不到。"""
    digits = _digits(pub)
    ptype = infer_patent_type_from_pub(pub) or TYPE_ALL
    boxes = crawler.wanted_boxes(epub_query_types(ptype))
    for query in _query_order(pub):
        if not query:
            continue
        if not crawler._can_search_here(page):
            crawler.wait_for_epub_home_ready(page, deadline=deadline)
        crawler.submit_index_search(page, query, boxes=boxes, deadline=deadline, cap_ms=crawler.TERM_STEP_CAP_MS)
        try:
            page.wait_for_load_state("load", timeout=crawler._timeout_ms(deadline, crawler.AJAX_CAP_MS))
        except Exception:  # noqa: BLE001, S110 —— 外链资源挂着不算数，靠间隔兜底
            pass
        if not page.query_selector("#sizeSelect"):          # 无查询结果页
            continue
        found = _find_item(page, digits)
        if found:
            return found
        # 类型未知时勾了全部、落在第一个页签：到别的页签上找
        for _label, el, current in crawler._tabs(page):
            if current:
                continue
            page.wait_for_timeout(int(crawler.AJAX_PACING_SEC * 1000))
            crawler._ajax(page, el.click, deadline=deadline)
            found = _find_item(page, digits)
            if found:
                return found
    return None


def _open_viewer(context: Any, item: Any, *, deadline: float | None) -> tuple[Any, str]:
    """点条目上的单行本按钮，等阅读器弹窗里出现 iframe；返回 ``(弹窗, iframe 地址)``。"""
    btn = item.query_selector(".func a.btn[title]")
    if btn is None:
        raise RuntimeError("条目上没有单行本按钮")
    with context.expect_page(timeout=crawler._timeout_ms(deadline, VIEWER_CAP_MS)) as info:
        btn.click()
    popup = info.value
    limit = time.monotonic() + crawler._timeout_ms(deadline, VIEWER_CAP_MS) / 1000
    src = None
    while time.monotonic() < limit:
        try:
            src = popup.evaluate(
                "() => { const f = document.querySelector('iframe, embed'); return f ? (f.getAttribute('src') || f.src) : null; }"
            )
        except Exception:  # noqa: BLE001 —— 弹窗还在跳转
            src = None
        # 阅读器地址由页面脚本稍后填进 iframe；填之前 src 为空，读到的是弹窗自己的地址
        if src and "path=" in str(src):
            return popup, str(src)
        popup.wait_for_timeout(1000)
    popup.close()
    raise RuntimeError("单行本阅读器没有打开")


def _download_front_page(context: Any, src: str, out_path: Path, *, deadline: float | None) -> int:
    """阅读器 showPage → 第 1 页图；返回总页数。"""
    parts = urlsplit(src)
    m = re.search(r"(?:^|&)path=([^&]+)", parts.query)
    if not m:
        raise RuntimeError(f"阅读器地址里没有 path 参数：{src[:120]}")
    base = f"{parts.scheme}://{parts.netloc}"
    timeout = crawler._timeout_ms(deadline, VIEWER_CAP_MS)
    resp = context.request.get(
        f"{base}/showPage?path={m.group(1)}&page=1",
        headers={"Referer": src, "X-Requested-With": "XMLHttpRequest"},
        timeout=timeout,
    )
    if resp.status != 200:
        raise RuntimeError(f"阅读器 showPage HTTP {resp.status}")
    model = resp.json()
    img_path = str((model or {}).get("pdfPath") or "")
    if not img_path:
        raise RuntimeError("阅读器说文件不存在")
    img = context.request.get(f"{base}/imgs/{img_path}", headers={"Referer": src}, timeout=timeout)
    body = img.body()
    if img.status != 200 or not body.startswith(IMAGE_MAGIC):
        raise RuntimeError(f"扉页图取不到（HTTP {img.status}）")
    out_path.write_bytes(body)
    try:
        return int(model.get("pdfNum") or 0)
    except (TypeError, ValueError):
        return 0


def fetch_front_pages(
    pubs: list[str],
    out_dir: Path,
    *,
    deadline: float | None = None,
    on_event: EventCallback | None = None,
    playwright_factory: Callable[[], Any] | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """一个浏览器、只过一次防护挑战，逐篇取扉页图。返回 ``(取到的, 被拦截的原因或 None)``。"""
    emit = on_event or (lambda _kind, _data: None)
    results: list[dict[str, Any]] = []
    out_dir.mkdir(parents=True, exist_ok=True)
    with (playwright_factory or crawler.sync_playwright)() as p:
        browser, label = crawler._launch_browser_labeled(p)
        context = crawler._new_context(browser, label)
        try:
            page = context.new_page()
            t0 = time.monotonic()
            try:
                crawler.wait_for_epub_home_ready(
                    page, deadline=deadline, on_wait=lambda sec: emit("wait", {"stage": "home", "sec": int(sec)})
                )
            except Exception as exc:  # noqa: BLE001 —— 首页门控失败 = 被拦截/不可达
                return results, crawler._short(exc)
            emit("home_ready", {"sec": round(time.monotonic() - t0, 1)})
            for i, pub in enumerate(pubs):
                if deadline is not None and deadline - time.monotonic() < MIN_PUB_SEC:
                    for left in pubs[i:]:
                        emit("page_failed", {"pub": left, "error": "时间预算用尽，未开始"})
                    break
                if i:
                    page.wait_for_timeout(int(crawler.TERM_PACING_SEC * 1000))
                started = time.monotonic()
                try:
                    found = _search_pub(page, pub, deadline=deadline)
                    if found is None:
                        raise RuntimeError("公布公告里没有这个公开号")
                    item, hit = found
                    popup, src = _open_viewer(context, item, deadline=deadline)
                    try:
                        out_path = out_dir / f"{_digits(pub) or 'pub'}-p1.png"
                        pages = _download_front_page(context, src, out_path, deadline=deadline)
                    finally:
                        popup.close()
                except Exception as exc:  # noqa: BLE001 —— 一篇失败不连累其它篇
                    emit("page_failed", {"pub": pub, "error": crawler._short(exc)})
                    continue
                data = {
                    "pub": pub,
                    "path": str(out_path),
                    "pages": pages,
                    "sec": round(time.monotonic() - started, 1),
                    "title": hit.title,
                    "applicant": hit.applicant,
                    "pub_date": hit.pub_date,
                    "abstract": hit.abstract,
                    "link": hit.link,
                }
                results.append(data)
                emit("page", data)
            return results, None
        finally:
            context.close()
            browser.close()


def _out(prefix: str, payload: Any) -> None:
    print(prefix, json.dumps(payload, ensure_ascii=False), flush=True)


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdio()
    argv = list(argv if argv is not None else sys.argv[1:])
    out_dir: Path | None = None
    pubs: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--out" and i + 1 < len(argv):
            out_dir = Path(argv[i + 1]).expanduser()
            i += 2
            continue
        if a.startswith("--out="):
            out_dir = Path(a.split("=", 1)[1]).expanduser()
        elif a.strip():
            pubs.append(a.strip())
        i += 1
    if out_dir is None or not pubs:
        print("usage: python cnipa_epub_front_page.py --out DIR <pub_no> [...]", file=sys.stderr)
        return 2

    os.environ.setdefault("EPUB_WAF_MAX_WAIT_SEC", "180")
    raw = os.environ.get("EPUB_DEADLINE_SEC", "").strip()
    try:
        sec = float(raw)
    except ValueError:
        sec = 0.0
    deadline = time.monotonic() + sec if sec > 0 else None

    def on_event(kind: str, data: dict[str, Any]) -> None:
        if kind == "page":
            _out("EPUB_PAGE_JSON:", data)
        elif kind == "page_failed":
            _out("EPUB_PAGE_FAIL_JSON:", data)
        elif kind == "home_ready":
            _out("EPUB_HOME_READY:", data)
        elif kind == "wait":
            _out("EPUB_WAIT:", data)

    try:
        results, blocked = fetch_front_pages(pubs, out_dir, deadline=deadline, on_event=on_event)
    except Exception as exc:  # noqa: BLE001 —— 浏览器起不来之类的环境错误
        print("CNIPA_EPUB_ERROR:", exc, file=sys.stderr, flush=True)
        return 1
    if blocked and not results:
        print("CNIPA_EPUB_ERROR:", blocked, file=sys.stderr, flush=True)
        return 3
    print(f"EPUB_PAGES_DONE: {len(results)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
