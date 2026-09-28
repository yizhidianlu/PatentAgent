# -*- coding: utf-8 -*-
"""
国知局公布站「检索 + 解析」一步完成：内存中持有结果页 HTML，**默认不落盘**。

内部调用 ``cnipa_epub_crawler.search_epub_keyword``（等同先 ``fetch_epub_result_html`` 再
``parse_search_result_html``）。

**输出约定**（便于 Agent 抓取且不触发误判降级）：

- **stdout**：机读行，每行一个前缀 + JSON（UTF-8）。**最终结果永远是 ``EPUB_HITS_JSON:`` 那一行**
  （合并去重后的全部命中；旧消费方只认这一行即可，其余行按前缀忽略）。其余行随检索进行逐行打出：

  - ``EPUB_WAIT:``           等待防护挑战中（``{"stage":"home","sec":15}``），证明「还在等」
  - ``EPUB_HOME_READY:``     首页防护已通过（``{"sec":8.6}``）
  - ``EPUB_TERM_JSON:``      某个词完成（``{"i":1,"n":7,"term":…,"sec":…,"hits":[…]}``）
  - ``EPUB_TERM_FAIL_JSON:`` 某个词失败（``{"i":…,"n":…,"term":…,"error":…}``），会继续下一个词
  - ``EPUB_SUMMARY_JSON:``   收尾摘要（``{"searched":[…],"failed":[…],"skipped":[…],"stop":…,"error":…}``）

  逐词行的意义：父进程被迫强杀子进程时，**已完成词的命中仍可从这些行里抢救出来**。
- **stderr**：``EPUB_MERGE:`` / ``EPUB_NOTE:`` / ``EPUB_HINT:`` 等为 **ASCII**。
- **退出码**：0 = 至少检索完成了一个词（可能是部分完成，见摘要）；3 = 首页防护未通过（被拦截/不可达）；
  1 = 其它错误（浏览器起不来等）；2 = 参数错误。

**时间预算**：环境变量 ``EPUB_DEADLINE_SEC``（秒，自脚本启动起算）。给了就按截止时刻收口：
来不及的词如实列为 skipped，已完成的词照常输出。

**专利类型**：``--type invention|utility_model|design|all``（默认 ``all``）。
对应首页勾选：发明公布+发明授权 / 实用新型 / 外观设计（见 ``tools/shared/patent_type.py``）。

用法：

  python tools/crawl/cnipa_epub_search.py 词1
  python tools/crawl/cnipa_epub_search.py --type utility_model 卡扣
  python tools/crawl/cnipa_epub_search.py --type design 外壳造型

需已安装：``pip install playwright``（或根目录 ``requirements.txt``）。有系统 Chrome / Edge 时不必 ``playwright install chromium``。探测：``python tools/shared/browser.py --probe``。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

_CRAWL = Path(__file__).resolve().parent
_SHARED = _CRAWL.parent / "shared"
for p in (_CRAWL, _SHARED):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)

from patent_type import TYPE_ALL, normalize_patent_type
from stdio_utf8 import ensure_utf8_stdio

_MAX_TERMS = 8


def _parse_argv(argv: list[str]) -> tuple[str, list[str]]:
    patent_type = TYPE_ALL
    rest: list[str] = []
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
        rest.append(a)
        i += 1
    return patent_type, rest


def _terms_from_argv(argv: list[str]) -> list[str]:
    terms: list[str] = []
    for a in argv:
        for part in (a or "").split():
            p = part.strip()
            if p:
                terms.append(p)
    return terms


def _dedupe_hits(hits_lists: list) -> list:
    from cnipa_epub_parse import EpubSearchHit

    seen: set[str] = set()
    out: list[EpubSearchHit] = []
    for hits in hits_lists:
        for h in hits:
            key = h.pub_number or h.link or (h.title or "")[:120]
            if key in seen:
                continue
            seen.add(key)
            out.append(h)
    return out


def _deadline_from_env() -> float | None:
    raw = os.environ.get("EPUB_DEADLINE_SEC", "").strip()
    try:
        sec = float(raw)
    except ValueError:
        return None
    return time.monotonic() + sec if sec > 0 else None


def _out(prefix: str, payload: Any) -> None:
    print(prefix, json.dumps(payload, ensure_ascii=False), flush=True)


def _usage() -> None:
    print(
        "usage: python tools/crawl/cnipa_epub_search.py [--type invention|utility_model|design|all] <term> [...]",
        file=sys.stderr,
    )
    print(
        "whitespace splits to multiple terms; one browser for all terms; merge by pub_number.",
        file=sys.stderr,
    )
    print(
        'example: python tools/crawl/cnipa_epub_search.py --type utility_model 卡扣',
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdio()
    argv = argv if argv is not None else sys.argv[1:]
    patent_type, rest = _parse_argv(argv)
    terms = _terms_from_argv(rest)
    if not terms:
        _usage()
        return 2
    if len(terms) > _MAX_TERMS:
        print(
            "ERROR: too many terms after split (%d > %d); shorten or run in batches."
            % (len(terms), _MAX_TERMS),
            file=sys.stderr,
        )
        return 2

    os.environ.setdefault("EPUB_WAF_MAX_WAIT_SEC", "180")

    try:
        import playwright
    except ImportError:
        print(
            "ERROR: pip install playwright  (or: pip install -r requirements.txt)",
            file=sys.stderr,
        )
        print(
            "HINT: python tools/shared/browser.py --probe",
            file=sys.stderr,
        )
        return 1

    from cnipa_epub_crawler import run_epub_session
    from cnipa_epub_parse import hits_to_jsonable

    multi = len(terms) > 1
    deadline = _deadline_from_env()

    def on_event(kind: str, data: dict[str, Any]) -> None:
        if kind == "term":
            _out("EPUB_TERM_JSON:", {**data, "hits": hits_to_jsonable(data.get("hits") or [])})
        elif kind == "term_failed":
            _out("EPUB_TERM_FAIL_JSON:", data)
        elif kind == "home_ready":
            _out("EPUB_HOME_READY:", data)
        elif kind == "wait":
            _out("EPUB_WAIT:", data)

    try:
        run = run_epub_session(terms, patent_type=patent_type, deadline=deadline, on_event=on_event)
    except Exception as e:
        print("CNIPA_EPUB_ERROR:", e, file=sys.stderr)
        return 1

    _out(
        "EPUB_SUMMARY_JSON:",
        {
            "searched": run.searched,
            "failed": [term for term, _reason in run.failed],
            "skipped": run.skipped,
            "stop": run.stop,
            "error": run.error,
        },
    )
    if run.stop == "blocked" and not run.rows:
        print("CNIPA_EPUB_ERROR:", run.error, file=sys.stderr, flush=True)
        return 3

    rows = [(html, hits) for _term, html, hits in run.rows]
    last_html = rows[-1][0] if rows else ""
    all_batches = [hits for _html, hits in rows]

    if not all_batches:
        # 首页过了但一个词都没完成（全失败或预算极紧）：仍给出空结果行，原因在摘要里
        hits = []
        print("EPUB_NOTE: no term completed stop=%s" % (run.stop or "-"), file=sys.stderr, flush=True)
    elif multi:
        hits = _dedupe_hits(all_batches)
        print(
            "EPUB_MERGE: terms=%d searched=%d type=%s merged_hits=%d"
            % (len(terms), len(rows), patent_type, len(hits)),
            file=sys.stderr,
            flush=True,
        )
    else:
        hits = all_batches[0]
        print(
            "EPUB_NOTE: type=%s" % patent_type,
            file=sys.stderr,
            flush=True,
        )

    if not hits and last_html and len(last_html) < 20_000:
        print(
            "EPUB_HINT: 0 hits; try broader terms, --type all, or WebSearch",
            file=sys.stderr,
            flush=True,
        )

    print(
        "EPUB_NOTE: html_bytes=%d disk=0" % len(last_html),
        file=sys.stderr,
        flush=True,
    )
    print(
        "EPUB_HITS_JSON:",
        json.dumps(hits_to_jsonable(hits), ensure_ascii=False),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
