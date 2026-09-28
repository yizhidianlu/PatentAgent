"""国知局（CNIPA）公布公告查新服务（prompt-porting-spec §2 A4 / R8）。

子进程调移植脚本 `app/tools/cnipa_epub_search.py`（Playwright，浏览器顺序
chrome → msedge → chromium，见 `tools/browser.py`），**流式**读其机读 stdout 协议：

    EPUB_WAIT: / EPUB_HOME_READY: / EPUB_TERM_JSON: / EPUB_TERM_FAIL_JSON:   运行中逐行
    EPUB_SUMMARY_JSON:                                                         收尾摘要
    EPUB_HITS_JSON: + JSON 数组（[{title, pub_number, link, abstract}]）      最终结果
    其余为 ASCII 的 EPUB_MERGE: / EPUB_NOTE: / EPUB_HINT: 提示（stderr 已合并进 stdout）

铁律：

- **URL 照抄**：`search_hits.url` 一律取条目的 `link` 字段，缺 link 的条目直接丢弃
  （宁可少一条，也不给下游拼一个能编造的 URL）；
- **失败即降级**：全部返回 `status='failed'` 并写库，**绝不抛异常、绝不阻塞流水线**——
  由 A4 的门控（重试 / 用户粘贴在先文献 / 跳过并如实写明未检索）兜底；
- **失败要说真话**：被拦截（blocked）、超预算（budget）、零命中（empty）、脚本错误（script）
  处置完全相反，报错必须分开说。早先它们共用「检索超时，疑似 WAF 拦截或网络不可达」一句话，
  而那次的真实情况是站点正常、检索正在成功进行，只是 7 个词需要 248 秒、预算只给了 180 秒；
- **已完成的永远保留**：预算按词数伸缩；来不及的词如实列为未检索，完成的词照常入库；
- **进度即心跳**：逐词进度推到界面，同时刷新流水线的「卡住」计时——国知局子进程不是 LLM 调用，
  不会出现在在途登记里，没有这条通路，检索跑到第 90 秒界面就会劝用户取消；
- **6 小时缓存**：同 terms+type（归一化哈希）命中 6 小时内的**完整**成功会话即复用
  （部分完成的会话不进缓存，否则会被当成完整结果复用）。

本模块只做「子进程 + 落库 + 进度回调」，不调 LLM。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import queue
import re
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

import httpx
from ulid import ULID

from ..config import get_config
from ..db import database as db
from ..models.search import (
    MAX_TERMS,
    BrowserProbe,
    ManualHitIn,
    SearchHit,
    SearchQuery,
    SearchResult,
    hit_row_to_model,
)
from . import ocr as ocr_service
from . import patent_fetch
from . import progress as progress_service
from .convert import kill_process_tree, run_tool, spawn_tool
from .sse import hub

logger = logging.getLogger(__name__)

# 检索脚本 / 探测脚本
SEARCH_SCRIPT = "cnipa_epub_search.py"
# 按公开号取单行本扉页图（Google 拿不到、又没有 PDF 链接的老文献，OCR 认摘要）
FRONT_PAGE_SCRIPT = "cnipa_epub_front_page.py"
BROWSER_SCRIPT = "browser.py"

# 检索时间预算（秒）= 基础 + 每词 × 词数，封顶。
#
# 早先是**一个写死的 180s 套在整场多词检索上**，而脚本对每个词都回首页重过一遍防护挑战，
# 一词约 35s：7 个词实测 248s，于是每次都在第 180s 被杀、全部作废。现在第 2 个词起在结果页上
# 直接检索（一词约 2~8s），预算又随词数伸缩——两道保险，任何一道单独都不够：
# 结果页复用若失效会退回「每词回首页」，那时仍要靠预算够宽 + 部分结果兜底。
SEARCH_BUDGET_BASE = 60        # 浏览器冷启动 + 首页 + 过一次防护挑战，实测 10~25s
# 每个词现在要读两个类型页签、每页 10 条、按需翻页（每次页内操作约 1~3s，另有 1.5s 间隔），
# 实测一词 8~30s。原先的 25s/词是按「每词只取 3 条」定的：真实案件里 6 个词 210s 的预算
# 只做完 4 个，其中两个词的实用新型页签因预算用尽没读，漏掉了三篇最相关的实用新型。
SEARCH_BUDGET_PER_TERM = 45
SEARCH_BUDGET_CAP = 480
# 预算到点后子进程收尾（完成当前这一步、关浏览器、打出结果）的宽限；超过才强杀整棵进程树
TEARDOWN_GRACE_SEC = 30
# 首页防护挑战的最长等待；实测 5~15s 通过，等 90s 还不过基本就是被拦了
WAF_MAX_WAIT_SEC = 90
# 浏览器探测超时（秒）：含冷启动
PROBE_TIMEOUT = 120
# 结果缓存有效期（小时）
CACHE_TTL_HOURS = 6

_HITS_MARKER = "EPUB_HITS_JSON:"
_SUMMARY_MARKER = "EPUB_SUMMARY_JSON:"
_TERM_MARKER = "EPUB_TERM_JSON:"
_TERM_FAIL_MARKER = "EPUB_TERM_FAIL_JSON:"
_HOME_READY_MARKER = "EPUB_HOME_READY:"
_WAIT_MARKER = "EPUB_WAIT:"
# 脚本退出码 3 = 首页防护未通过
_EXIT_BLOCKED = 3


def search_budget(n_terms: int) -> int:
    """一场 n 个词的检索给多少秒。"""
    n = max(1, int(n_terms or 1))
    return min(SEARCH_BUDGET_CAP, SEARCH_BUDGET_BASE + SEARCH_BUDGET_PER_TERM * n)

# stderr 里对人有用的提示行前缀
_STDERR_KEEP = ("EPUB_", "CNIPA_EPUB_ERROR", "ERROR", "BROWSER:", "HINT")

# 合法的 --type 取值（tools/patent_type.py 的规范名）
_TYPE_ALIASES = {
    "invention": "invention",
    "发明": "invention",
    "utility_model": "utility_model",
    "utility-model": "utility_model",
    "实用新型": "utility_model",
    "design": "design",
    "外观设计": "design",
    "外观": "design",
    "all": "all",
    "全部": "all",
    "invention_utility_model": "invention_utility_model",
    "发明+实用新型": "invention_utility_model",
}

# 给人看的检索范围
TYPE_LABELS = {
    "invention": "发明",
    "utility_model": "实用新型",
    "design": "外观设计",
    "all": "全部类型",
    "invention_utility_model": "发明+实用新型",
}

# 取单行本扉页图：一场的时间预算 = 基数 + 每篇；最多取几篇。
# 真站点实测一篇 40~70s（检索、弹窗、阅读器起来、取图各要几秒到几十秒），外加一次首页挑战。
FRONT_PAGE_BASE_SEC = 45
FRONT_PAGE_PER_PUB_SEC = 75
MAX_FRONT_PAGE_PUBS = 3
_PAGE_MARKER = "EPUB_PAGE_JSON:"
_PAGE_FAIL_MARKER = "EPUB_PAGE_FAIL_JSON:"

# 一场检索入库的上限。逐类检索后每个词每类最多 3 条，8 个词、两类可达四十多条；消化改写
# 每 8 条一次 LLM 调用，全收会把这一步拖得很长，而排在后面的多是只在申请人、地址这类字段里
# 撞上检索词的条目。
MAX_SEARCH_HITS = 30

# 进度回调：cb(stage, msg) —— 同步或协程皆可
ProgressCallback = Callable[[str, str], Awaitable[None] | None]


# ---------------------------------------------------------------------------
# 进度回调工具
# ---------------------------------------------------------------------------


async def _notify(cb: ProgressCallback | None, stage: str, msg: str) -> None:
    """调用进度回调（兼容同步/异步；回调自身出错不影响检索）。"""
    if cb is None:
        return
    try:
        result = cb(stage, msg)
        if asyncio.iscoroutine(result):
            await result
    except Exception as exc:  # noqa: BLE001 —— 进度上报失败不该拖垮检索
        logger.warning("search_progress 回调失败：%s", exc)


def hub_progress(case_id: str, step_key: str | None = None) -> ProgressCallback:
    """构造把进度推成 SSE `search_progress` 的回调（流水线/API 共用）。

    字段名必须是 `message`：前端 SearchProgressEvent 就是这么定义的，
    而 sessionStore 的分支第一句是 `if (!d?.message) return`——
    早先这里发的是 `{stage, msg}`，于是国知局检索的每一条滚动进度都被静默丢弃，
    检索那几分钟界面上什么都不会出现。同一个事件在 oa 那条链路上带的正是 `message`，
    所以只有这一处是哑的。

    persist=False：滚动进度是瞬时值，UI 语义是「同一行原地更新」。
    落库的话界面只留一条、库里却存了几十条，重放时还会把它们一条条铺开。

    每条进度同时 touch 流水线的进度计时：流水线的「卡住」判定只认 LLM 在途登记，
    而国知局子进程不是 LLM 调用——不 touch 的话，一场三分钟的检索在第 90 秒就会被
    界面判成「长时间无反馈，可取消本步骤后重试」，而它明明正一个词一个词地往前走。
    """

    async def _cb(stage: str, msg: str) -> None:
        progress_service.touch(case_id, msg)
        await hub.emit(
            case_id,
            "search_progress",
            {"message": msg, "phase": stage},
            step_key=step_key,
            persist=False,
        )

    return _cb


# ---------------------------------------------------------------------------
# 检索词与类型归一化
# ---------------------------------------------------------------------------


def normalize_type(patent_type: str | None) -> str:
    """专利类型 → 脚本 `--type` 参数（未知一律 all，绝不炸）。"""
    raw = str(patent_type or "").strip().lower()
    return _TYPE_ALIASES.get(raw, _TYPE_ALIASES.get(str(patent_type or "").strip(), "all"))


def prior_art_scope(case_type: str | None) -> str:
    """案件专利类型 → 查新检索范围。

    现有技术不分专利类型：实用新型案件同样要和发明公布比，发明案件也要和实用新型比。
    早先按案件类型只勾一类，实用新型案件把全部发明公布挡在了门外——2026-09-28 的真实
    案件里，用户自己在 Google 上找到的两篇最相关文献都是发明公布，平台从一开始就不可能
    检到。外观设计比的是设计本身，仍只检外观设计。
    """
    return "design" if normalize_type(case_type) == "design" else "invention_utility_model"


def hit_relevance(title: str | None, abstract: str | None, terms: Sequence[str]) -> int:
    """与检索词的重合度：词出现在标题里计 2 分、只在摘要里计 1 分（每词只计一次）。"""
    t, a = str(title or ""), str(abstract or "")
    return sum(2 if x in t else 1 if x in a else 0 for x in terms if x)


def rank_hits(hits: Sequence[Mapping[str, Any]], terms: Sequence[str]) -> list[dict[str, Any]]:
    """按与检索词的重合度排序，同分保持原顺序。

    公布站是多字段「或」检索，只在申请人、地址、代理机构里撞上检索词的条目也会进结果；
    它们与方案无关，应该排在后面、超上限时先被舍弃。
    """
    return sorted(
        (dict(h) for h in hits),
        key=lambda h: hit_relevance(h.get("title"), h.get("abstract"), terms),
        reverse=True,
    )


def rank_hit_models(hits: Sequence[SearchHit], terms: Sequence[str]) -> list[SearchHit]:
    """同 `rank_hits`，作用于入库后的命中对象。"""
    return sorted(hits, key=lambda h: hit_relevance(h.title, h.abstract, terms), reverse=True)


def normalize_terms(terms: Iterable[str] | None) -> list[str]:
    """检索词归一化：按空白切分、去空、有序去重，最多 `MAX_TERMS` 个。

    脚本按空白把参数再切一次，故此处提前切好并截断，避免脚本以退出码 2 拒绝。
    """
    out: list[str] = []
    for term in terms or []:
        for part in re.split(r"\s+", str(term or "").strip()):
            p = part.strip()
            if p and p not in out:
                out.append(p)
    return out[:MAX_TERMS]


def terms_key(terms: Sequence[str], patent_type: str) -> str:
    """缓存键：归一化词集（顺序无关）+ 类型 的 sha256。"""
    payload = json.dumps(
        {"terms": sorted(set(terms)), "type": normalize_type(patent_type)}, ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 子进程调用
# ---------------------------------------------------------------------------


def _browser_env() -> dict[str, str]:
    """settings.general.browser_channel → 透传给 tools/browser.py（未配置则自动探测）。"""
    try:
        general = db.get_setting_json("general") or {}
        channel = str(general.get("browser_channel") or "").strip().lower()
        if channel in ("chrome", "msedge", "chromium"):
            return {"PATENT_BROWSER_CHANNEL": channel}
    except Exception as exc:  # noqa: BLE001 —— DB 未初始化不阻塞检索
        logger.debug("读取 browser_channel 设置失败，走自动探测：%s", exc)
    return {}


def _stderr_tail(stderr: str, limit: int = 6) -> str:
    """截取 stderr 中的机读/提示行，作为失败原因说明。"""
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    keep = [ln for ln in lines if ln.startswith(_STDERR_KEEP)] or lines
    return " | ".join(keep[-limit:])[:1000]


def parse_hits_stdout(stdout: str) -> list[dict[str, Any]] | None:
    """从 stdout 中取唯一一行 `EPUB_HITS_JSON:`；无该行或非法 JSON 返回 None。"""
    for line in (stdout or "").splitlines():
        s = line.strip()
        if not s.startswith(_HITS_MARKER):
            continue
        payload = s[len(_HITS_MARKER):].strip()
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            return None
        return data if isinstance(data, list) else None
    return None


def normalize_hits(rows: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """脚本条目 → 入库形状 `[{pub_no,title,abstract,applicant,pub_date,url}]`。

    返回 `(hits, dropped)`：**没有 link 的条目一律丢弃**（URL 硬规则），
    并按 url 去重（脚本已按 pub_number 合并，此处再兜一层）。
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    dropped = 0
    for row in rows or []:
        if not isinstance(row, Mapping):
            dropped += 1
            continue
        url = str(row.get("link") or row.get("url") or "").strip()
        if not url:
            dropped += 1
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(
            {
                "pub_no": (str(row.get("pub_number") or row.get("pub_no") or "").strip() or None),
                "title": (str(row.get("title") or "").strip() or None),
                "abstract": (str(row.get("abstract") or "").strip() or None),
                "applicant": (str(row.get("applicant") or "").strip() or None),
                "pub_date": (str(row.get("pub_date") or "").strip() or None),
                "url": url,
            }
        )
    return out, dropped


def probe_browser_sync(timeout: int = PROBE_TIMEOUT) -> BrowserProbe:
    """探测可用浏览器 channel（本机 Chrome → Edge → 自带 Chromium）。

    子进程调 `tools/browser.py --probe`，stdout 一行 JSON。任何失败都返回
    `ok=False` + 原因，不抛异常（查新是否可跑由调用方据此决定是否直接转人工兜底）。
    """
    try:
        proc = run_tool(BROWSER_SCRIPT, ["--probe"], timeout=timeout, extra_env=_browser_env())
    except subprocess.TimeoutExpired:
        return BrowserProbe(ok=False, error=f"浏览器探测超时（{timeout}s）")
    except OSError as exc:
        return BrowserProbe(ok=False, error=f"无法启动探测脚本：{exc}")

    for line in reversed((proc.stdout or "").splitlines()):
        s = line.strip()
        if not s.startswith("{"):
            continue
        try:
            return BrowserProbe.model_validate(json.loads(s))
        except (json.JSONDecodeError, ValueError):
            continue
    return BrowserProbe(ok=False, error=_stderr_tail(proc.stderr) or f"探测脚本退出码 {proc.returncode}")


async def probe_browser(timeout: int = PROBE_TIMEOUT) -> BrowserProbe:
    """`probe_browser_sync` 的异步包装（丢线程池，不阻塞事件循环）。"""
    return await db.arun(probe_browser_sync, timeout)


LineCallback = Callable[[str], None]


def _json_after(line: str, marker: str) -> Any:
    """取 `marker` 之后的 JSON；解析失败返回 None（坏行不拖垮整次解析）。"""
    try:
        return json.loads(line[len(marker):].strip())
    except (json.JSONDecodeError, ValueError):
        return None


def parse_search_protocol(lines: Sequence[str]) -> dict[str, Any]:
    """把脚本输出逐行归类：结果、摘要、逐词完成/失败、首页是否已过防护。"""
    out: dict[str, Any] = {
        "hits": None,          # 最终 EPUB_HITS_JSON（合并去重后的全部命中）
        "summary": None,       # EPUB_SUMMARY_JSON
        "terms": [],           # 逐词完成 [{i,n,term,sec,hits}]
        "term_fails": [],      # 逐词失败 [{i,n,term,error}]
        "home_ready": False,
        "notes": [],           # 其余行（提示、报错、浏览器信息）
    }
    for raw in lines:
        s = raw.strip()
        if not s:
            continue
        if s.startswith(_HITS_MARKER):
            data = _json_after(s, _HITS_MARKER)
            out["hits"] = data if isinstance(data, list) else None
        elif s.startswith(_SUMMARY_MARKER):
            data = _json_after(s, _SUMMARY_MARKER)
            out["summary"] = data if isinstance(data, dict) else None
        elif s.startswith(_TERM_FAIL_MARKER):
            data = _json_after(s, _TERM_FAIL_MARKER)
            if isinstance(data, dict):
                out["term_fails"].append(data)
        elif s.startswith(_TERM_MARKER):
            data = _json_after(s, _TERM_MARKER)
            if isinstance(data, dict):
                out["terms"].append(data)
        elif s.startswith(_HOME_READY_MARKER):
            out["home_ready"] = True
        elif s.startswith(_WAIT_MARKER):
            continue
        else:
            out["notes"].append(s)
    # 逐词行出现过，说明首页必然已通过
    if out["terms"] or out["term_fails"]:
        out["home_ready"] = True
    return out


def _type_gaps(terms: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, str]]:
    """逐词行里「这个词做完了，但某一类没检成」的记录：{词: {类型: 原因}}。"""
    gaps: dict[str, dict[str, str]] = {}
    for t in terms:
        errors = t.get("type_errors")
        if t.get("term") and isinstance(errors, Mapping) and errors:
            gaps[str(t["term"])] = {str(k): str(v) for k, v in errors.items()}
    return gaps


def parse_front_page_protocol(lines: Sequence[str]) -> dict[str, dict[str, Any]]:
    """扉页脚本的输出 → {公开号: 数据}（失败的条目只有 error）。"""
    out: dict[str, dict[str, Any]] = {}
    for raw in lines:
        s = raw.strip()
        if s.startswith(_PAGE_MARKER):
            data = _json_after(s, _PAGE_MARKER)
            if isinstance(data, dict) and data.get("pub"):
                out[str(data["pub"])] = data
        elif s.startswith(_PAGE_FAIL_MARKER):
            data = _json_after(s, _PAGE_FAIL_MARKER)
            if isinstance(data, dict) and data.get("pub"):
                out.setdefault(str(data["pub"]), {"pub": data["pub"], "error": str(data.get("error") or "失败")})
    return out


def _run_front_page_script(pubs: Sequence[str], out_dir: Path, budget: int, on_line: LineCallback | None) -> dict[str, dict[str, Any]]:
    """跑一次扉页脚本（同步；供线程池调用）。每个公开号都有交代：拿到图，或失败原因。"""
    pubs = list(pubs)
    args = ["--out", str(out_dir), *pubs]
    env = _browser_env()
    env["EPUB_DEADLINE_SEC"] = str(budget)
    env["EPUB_WAF_MAX_WAIT_SEC"] = str(min(WAF_MAX_WAIT_SEC, budget))
    try:
        lines, rc, killed = _stream_script(args, env, budget + TEARDOWN_GRACE_SEC, on_line, script=FRONT_PAGE_SCRIPT)
    except OSError as exc:
        return {pub: {"pub": pub, "error": f"无法启动脚本：{exc}"} for pub in pubs}
    out = parse_front_page_protocol(lines)
    if killed:
        reason = f"{budget}s 内没有完成"
    elif rc == _EXIT_BLOCKED:
        reason = "国知局访问验证未通过"
    else:
        notes = [ln for ln in lines if ln.startswith(("CNIPA_EPUB_ERROR", "ERROR"))]
        reason = (notes[-1][:200] if notes else f"脚本退出码 {rc}") if rc not in (0, None) else "脚本没有交代这一篇"
    for pub in pubs:
        out.setdefault(pub, {"pub": pub, "error": reason})
    return out


async def fetch_front_pages(
    pubs: Sequence[str], *, on_progress: ProgressCallback | None = None
) -> dict[str, dict[str, Any]]:
    """到国知局公布公告取这些公开号的单行本扉页图（子进程 + 浏览器，一次挑战多篇）。"""
    pubs = [p for p in dict.fromkeys(str(x).strip() for x in pubs) if p]
    if not pubs:
        return {}
    out_dir = get_config().tmp_dir / "epub_front_pages"
    out_dir.mkdir(parents=True, exist_ok=True)
    budget = FRONT_PAGE_BASE_SEC + FRONT_PAGE_PER_PUB_SEC * len(pubs)
    loop = asyncio.get_running_loop()

    def on_line(line: str) -> None:
        s = line.strip()
        msg = _progress_message(s)
        if s.startswith(_PAGE_MARKER):
            d = _json_after(s, _PAGE_MARKER) or {}
            msg = f"已取到「{d.get('title') or d.get('pub')}」的扉页图（{d.get('sec')}s）"
        elif s.startswith(_PAGE_FAIL_MARKER):
            d = _json_after(s, _PAGE_FAIL_MARKER) or {}
            msg = f"「{d.get('pub')}」的扉页图取不到：{d.get('error')}"
        if msg and on_progress is not None:
            asyncio.run_coroutine_threadsafe(_notify(on_progress, "enrich", msg), loop)

    return await db.arun(_run_front_page_script, pubs, out_dir, budget, on_line)


def _merge_term_hits(terms: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """逐词行里的命中合并去重（强杀后抢救用；与脚本的合并口径一致：按公开号/链接）。"""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for t in terms:
        for h in t.get("hits") or []:
            if not isinstance(h, Mapping):
                continue
            key = str(h.get("pub_number") or h.get("link") or h.get("title") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(dict(h))
    return out


def _progress_message(line: str) -> str | None:
    """脚本的一行输出 → 给用户看的进度文案（不认识的行不上报）。"""
    s = line.strip()
    if s.startswith(_TERM_MARKER):
        d = _json_after(s, _TERM_MARKER) or {}
        n_hits = len(d.get("hits") or [])
        types = d.get("types") if isinstance(d.get("types"), dict) else {}
        gaps = d.get("type_errors") if isinstance(d.get("type_errors"), dict) else {}
        detail = (
            "、".join(f"{label} {n} 条" for label, n in types.items())
            if len(types) > 1 or gaps
            else f"{n_hits} 条命中"
        )
        msg = f"已完成 {d.get('i')}/{d.get('n')}：「{d.get('term')}」{detail}（{d.get('sec')}s）"
        if gaps:
            msg += f"；{'、'.join(gaps)}未检成"
        return msg
    if s.startswith(_TERM_FAIL_MARKER):
        d = _json_after(s, _TERM_FAIL_MARKER) or {}
        return f"第 {d.get('i')}/{d.get('n')} 个词「{d.get('term')}」检索失败，继续下一个"
    if s.startswith(_HOME_READY_MARKER):
        d = _json_after(s, _HOME_READY_MARKER) or {}
        return f"已通过国知局访问验证（{d.get('sec')}s），开始逐词检索…"
    if s.startswith(_WAIT_MARKER):
        d = _json_after(s, _WAIT_MARKER) or {}
        return f"正在等待国知局访问验证（已等待 {d.get('sec')}s）…"
    return None


def _stream_script(
    args: list[str],
    env: dict[str, str],
    hard_limit: float,
    on_line: LineCallback | None,
    *,
    script: str = SEARCH_SCRIPT,
) -> tuple[list[str], int | None, bool]:
    """起子进程并逐行读，超过 `hard_limit` 秒强杀整棵进程树。返回 `(行, 退出码, 是否被强杀)`。

    读管道放在单独线程：主循环按剩余时间 `queue.get(timeout)`，这样子进程卡死不出声时
    也能按时强杀，而不是被一个阻塞的 readline 永远挂住。
    """
    proc = spawn_tool(script, args, extra_env=env)
    lines: list[str] = []
    q: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                q.put(line)
        except (OSError, ValueError):  # 管道在强杀后被关闭
            pass
        finally:
            q.put(None)

    reader = threading.Thread(target=pump, name="cnipa-search-reader", daemon=True)
    reader.start()

    def take(line: str) -> None:
        line = line.rstrip("\r\n")
        lines.append(line)
        if on_line is not None:
            try:
                on_line(line)
            except Exception as exc:  # noqa: BLE001 —— 进度上报失败不影响检索
                logger.debug("检索进度回调失败：%s", exc)

    killed = False
    end = time.monotonic() + hard_limit
    finished = False
    while not finished:
        left = end - time.monotonic()
        if left <= 0:
            killed = True
            kill_process_tree(proc)
            break
        try:
            item = q.get(timeout=min(left, 1.0))
        except queue.Empty:
            continue
        if item is None:
            finished = True
        else:
            take(item)

    if killed:
        # 强杀后把管道里残留的行读干净——已完成的词就在这些行里
        drain_end = time.monotonic() + 5.0
        while time.monotonic() < drain_end:
            try:
                item = q.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                break
            take(item)
    try:
        rc: int | None = proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        rc = None
    return lines, rc, killed


def _failure(kind: str, error: str, terms: Sequence[str], parsed: Mapping[str, Any] | None = None) -> dict[str, Any]:
    parsed = parsed or {}
    return {
        "ok": False,
        "kind": kind,
        "hits": [],
        "error": error,
        "searched": [],
        "skipped": list(terms),
        "failed": [],
        "stop": kind,
        "stderr": " | ".join((parsed.get("notes") or [])[-6:])[:1000],
    }


def _run_search_script(
    terms: Sequence[str],
    patent_type: str,
    budget: int,
    on_line: LineCallback | None = None,
) -> dict[str, Any]:
    """跑一次检索子进程（同步；供线程池调用）。

    返回 `{ok, kind, hits, error, searched, skipped, failed, stop, stderr}`：
    - ok=True 时 hits 为可入库的命中，**可能是部分完成**（skipped/failed 非空）；
    - ok=False 时 kind 说明是哪一类失败（blocked / budget / script），error 是给人看的原因。
    """
    terms = list(terms)
    args = ["--type", patent_type, *terms]
    env = _browser_env()
    env["EPUB_DEADLINE_SEC"] = str(budget)
    env["EPUB_WAF_MAX_WAIT_SEC"] = str(min(WAF_MAX_WAIT_SEC, budget))
    try:
        lines, rc, killed = _stream_script(args, env, budget + TEARDOWN_GRACE_SEC, on_line)
    except OSError as exc:
        return _failure("script", f"无法启动检索脚本：{exc}", terms)

    parsed = parse_search_protocol(lines)
    summary = parsed["summary"]
    tail = " | ".join(parsed["notes"][-6:])[:1000]

    # ---- 1) 脚本自己走完了收尾：摘要是权威说法 ----
    if summary is not None:
        searched = [str(t) for t in summary.get("searched") or []]
        skipped = [str(t) for t in summary.get("skipped") or []]
        failed = [str(t) for t in summary.get("failed") or []]
        stop = summary.get("stop")
        reason = str(summary.get("error") or "").strip()
        if rc == _EXIT_BLOCKED or (stop == "blocked" and not searched):
            return _failure(
                "blocked",
                f"国知局访问验证未通过，未能开始检索（{reason or '首页未出现检索框'}）。"
                "多为临时拦截或网络问题，稍后重试可能恢复",
                terms,
                parsed,
            )
        hits = parsed["hits"] if parsed["hits"] is not None else _merge_term_hits(parsed["terms"])
        return {
            "ok": True,
            "kind": None,
            "hits": hits,
            "error": None,
            "searched": searched,
            "skipped": skipped,
            "failed": failed,
            "stop": stop,
            "stderr": tail,
            "type_gaps": _type_gaps(parsed["terms"]),
        }

    # ---- 2) 没有摘要：被强杀或崩溃。先从逐词行里抢救已完成的词 ----
    done = [str(t.get("term")) for t in parsed["terms"] if t.get("term")]
    bad = [str(t.get("term")) for t in parsed["term_fails"] if t.get("term")]
    if done:
        return {
            "ok": True,
            "kind": None,
            "hits": _merge_term_hits(parsed["terms"]),
            "error": None,
            "searched": done,
            "skipped": [t for t in terms if t not in done and t not in bad],
            "failed": bad,
            "stop": "killed" if killed else "crashed",
            "stderr": tail,
            "type_gaps": _type_gaps(parsed["terms"]),
        }

    if killed:
        if parsed["home_ready"]:
            return _failure(
                "budget",
                f"检索超时：国知局已通过访问验证、站点正常，但 {budget}s 预算内没有完成任何一个检索词",
                terms,
                parsed,
            )
        return _failure(
            "blocked",
            f"检索超时：{budget}s 内未能通过国知局访问验证（可能被拦截或网络不通）",
            terms,
            parsed,
        )

    if rc not in (0, None):
        return _failure("script", f"检索脚本退出码 {rc}：{tail or '无输出'}", terms, parsed)
    if parsed["hits"] is None:
        return _failure("script", f"未解析到 EPUB_HITS_JSON 输出：{tail or '无输出'}", terms, parsed)
    # 旧协议（只有结果行、没有摘要）：视为全部检索完成
    return {
        "ok": True,
        "kind": None,
        "hits": parsed["hits"],
        "error": None,
        "searched": terms,
        "skipped": [],
        "failed": [],
        "stop": None,
        "stderr": tail,
    }


# ---------------------------------------------------------------------------
# 落库（同步 helper，统一经 db.arun 进线程池）
# ---------------------------------------------------------------------------


def _insert_query(
    case_id: str,
    *,
    source: str,
    patent_type: str | None,
    terms: Sequence[str],
    status: str,
    raw: Mapping[str, Any] | None = None,
    error: str | None = None,
) -> str:
    query_id = str(ULID())
    db.execute(
        """
        INSERT INTO search_queries(id, case_id, source, patent_type, terms_json, status,
                                   raw_json, error, created_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            query_id,
            case_id,
            source,
            patent_type,
            json.dumps(list(terms), ensure_ascii=False),
            status,
            json.dumps(dict(raw), ensure_ascii=False) if raw is not None else None,
            error,
            db.now_str(),
        ),
    )
    return query_id


def _update_query(
    query_id: str,
    *,
    status: str,
    raw: Mapping[str, Any] | None = None,
    error: str | None = None,
) -> None:
    db.execute(
        "UPDATE search_queries SET status=?, raw_json=COALESCE(?, raw_json), error=? WHERE id=?",
        (
            status,
            json.dumps(dict(raw), ensure_ascii=False) if raw is not None else None,
            error,
            query_id,
        ),
    )


def _existing_urls(case_id: str) -> set[str]:
    rows = db.query_all("SELECT url FROM search_hits WHERE case_id=?", (case_id,))
    return {r["url"] for r in rows}


def _insert_hits(
    case_id: str,
    query_id: str | None,
    hits: Iterable[Mapping[str, Any]],
    *,
    manual: bool = False,
) -> list[sqlite3.Row]:
    """插入命中（同案件内按 url 去重：已存在则复用旧行），返回全部相关行。"""
    known = _existing_urls(case_id)
    now = db.now_str()
    ids: list[str] = []
    urls: list[str] = []
    for hit in hits:
        url = str(hit.get("url") or "").strip()
        if not url:
            continue
        urls.append(url)
        if url in known:
            continue
        known.add(url)
        hit_id = str(ULID())
        ids.append(hit_id)
        db.execute(
            """
            INSERT INTO search_hits(id, query_id, case_id, pub_no, title, abstract, applicant,
                                    pub_date, url, selected, manual_entry, digest, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                hit_id,
                query_id,
                case_id,
                hit.get("pub_no"),
                hit.get("title"),
                hit.get("abstract"),
                hit.get("applicant"),
                hit.get("pub_date"),
                url,
                1 if hit.get("selected", True) else 0,
                1 if manual else 0,
                hit.get("digest"),
                now,
            ),
        )
    if not urls:
        return []
    placeholders = ",".join("?" * len(urls))
    return db.query_all(
        f"SELECT * FROM search_hits WHERE case_id=? AND url IN ({placeholders}) "
        "ORDER BY created_at ASC, rowid ASC",
        (case_id, *urls),
    )


def _hits_of_query(query_id: str) -> list[sqlite3.Row]:
    return db.query_all(
        "SELECT * FROM search_hits WHERE query_id=? ORDER BY created_at ASC, rowid ASC",
        (query_id,),
    )


def _cached_query(key: str, ttl_hours: int) -> sqlite3.Row | None:
    """按 terms_key 找 TTL 内最近一次成功的 cnipa 会话（跨案件复用）。"""
    # 本地朴素时间：与 db.now_str() 写入 created_at 的格式/时区口径保持一致
    cutoff = (datetime.now() - timedelta(hours=ttl_hours)).strftime("%Y-%m-%d %H:%M:%S")  # noqa: DTZ005
    rows = db.query_all(
        "SELECT * FROM search_queries WHERE source='cnipa' AND status='done' AND created_at>=? "
        "ORDER BY created_at DESC LIMIT 50",
        (cutoff,),
    )
    for row in rows:
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        # 部分完成的会话只覆盖了一部分词：当成这组词的完整结果复用，等于替用户假装检索过剩下的词
        if raw.get("partial"):
            continue
        if raw.get("terms_key") == key:
            return row
    return None


# ---------------------------------------------------------------------------
# 主入口：检索
# ---------------------------------------------------------------------------


async def search(
    case_id: str,
    terms: Iterable[str],
    patent_type: str = "invention",
    *,
    timeout: int | None = None,
    on_progress: ProgressCallback | None = None,
    use_cache: bool = True,
    cache_ttl_hours: int = CACHE_TTL_HOURS,
    empty_is_failure: bool = True,
) -> SearchResult:
    """跑一次国知局检索并落库，返回 `SearchResult`（**永不抛网络异常**）。

    参数
    ----
    terms            : 检索词（一次会话多词，脚本内共用一个浏览器；上限 8 个）。
    patent_type      : invention | utility_model | design | invention_utility_model | all
                       （其它值一律 all；查新请用 `prior_art_scope(案件类型)`）。
    timeout          : 时间预算（秒）；缺省按词数 `search_budget(n)`。
    on_progress      : `cb(stage, msg)`，stage ∈ start|cache|running|crawl|parsed|done|failed。
    use_cache        : 命中 6 小时内同 terms+type 的**完整**成功会话即复用。
    empty_is_failure : 零解析视为失败（默认 True，交人工兜底门控；A4 明确禁止编造检索结果）。

    部分完成（有词因预算用尽或失败没检索到）但有命中时，`status='done'`，
    `skipped_terms` / `failed_terms` 如实列出没检索的词；失败时 `failure_kind` 说明是哪一类。
    """
    started = time.monotonic()
    norm_terms = normalize_terms(terms)
    ptype = normalize_type(patent_type)

    if not norm_terms:
        error = "检索词为空"
        await _notify(on_progress, "failed", error)
        return SearchResult(
            status="failed", hits=[], error=error, terms=[], patent_type=ptype, failure_kind="script"
        )

    key = terms_key(norm_terms, ptype)
    await _notify(
        on_progress,
        "start",
        f"开始检索国知局公布公告：{'、'.join(norm_terms)}（范围：{TYPE_LABELS.get(ptype, ptype)}）",
    )

    # ---- 缓存 ----
    if use_cache:
        cached = await db.arun(_cached_query, key, cache_ttl_hours)
        if cached is not None:
            result = await _reuse_cached(case_id, cached, norm_terms, ptype, key)
            if result is not None:
                await _notify(
                    on_progress, "cache", f"复用 {cache_ttl_hours} 小时内的检索结果（{len(result.hits)} 条）"
                )
                result.elapsed_ms = int((time.monotonic() - started) * 1000)
                return result

    # ---- 真跑 ----
    query_id = await db.arun(
        lambda: _insert_query(
            case_id,
            source="cnipa",
            patent_type=ptype,
            terms=norm_terms,
            status="running",
            raw={"terms_key": key, "terms": norm_terms, "patent_type": ptype},
        )
    )
    budget = int(timeout) if timeout else search_budget(len(norm_terms))
    await _notify(
        on_progress,
        "running",
        f"正在检索 {len(norm_terms)} 个词（时间预算 {budget}s，复用本机浏览器过访问验证）…",
    )

    # 子进程在线程池里跑；它每打一行进度，就从那个线程把通知投递回事件循环
    loop = asyncio.get_running_loop()

    def on_line(line: str) -> None:
        msg = _progress_message(line)
        if msg and on_progress is not None:
            asyncio.run_coroutine_threadsafe(_notify(on_progress, "crawl", msg), loop)

    outcome = await db.arun(_run_search_script, norm_terms, ptype, budget, on_line)
    base_raw = {"terms_key": key, "terms": norm_terms, "patent_type": ptype, "budget": budget}

    def _elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    if not outcome["ok"]:
        error = str(outcome["error"])
        kind = outcome.get("kind") or "script"
        await db.arun(
            lambda: _update_query(
                query_id,
                status="failed",
                raw={**base_raw, "failure_kind": kind, "stderr": outcome.get("stderr") or ""},
                error=error,
            )
        )
        await _notify(on_progress, "failed", f"检索失败：{error}")
        logger.warning("CNIPA 检索失败 case=%s kind=%s：%s", case_id, kind, error)
        return SearchResult(
            status="failed",
            hits=[],
            error=error,
            query_id=query_id,
            terms=norm_terms,
            patent_type=ptype,
            elapsed_ms=_elapsed(),
            searched_terms=list(outcome.get("searched") or []),
            skipped_terms=list(outcome.get("skipped") or []),
            failed_terms=list(outcome.get("failed") or []),
            failure_kind=kind,
        )

    searched = [t for t in (outcome.get("searched") or norm_terms) if t]
    skipped = list(outcome.get("skipped") or [])
    failed = list(outcome.get("failed") or [])
    partial = bool(skipped or failed)
    pending_note = (
        f"另有 {len(skipped) + len(failed)} 个词未检索（{'、'.join([*skipped, *failed])}）"
        if partial
        else ""
    )

    hits, dropped = normalize_hits(outcome["hits"])
    hits = rank_hits(hits, norm_terms)
    trimmed = max(0, len(hits) - MAX_SEARCH_HITS)
    hits = hits[:MAX_SEARCH_HITS]
    type_gaps: dict[str, dict[str, str]] = dict(outcome.get("type_gaps") or {})
    await _notify(
        on_progress,
        "parsed",
        f"已检索 {len(searched)}/{len(norm_terms)} 个词，解析到 {len(hits) + trimmed} 条命中（丢弃无链接 {dropped} 条）"
        + (f"；按与检索词的重合度保留前 {MAX_SEARCH_HITS} 条" if trimmed else ""),
    )

    if not hits and empty_is_failure:
        if partial:
            # 做完的词都是零命中，但还有词没做：这是预算/拦截问题，不是「确实查不到」
            kind = "blocked" if outcome.get("stop") == "blocked" else "budget"
            error = (
                f"已检索的 {len(searched)} 个词均无命中；{pending_note}"
                + ("，检索中途被国知局拦截" if kind == "blocked" else "，时间预算已用尽")
            )
        else:
            kind = "empty"
            error = f"全部 {len(searched)} 个词检索完成，均无命中（原词重试结果不会不同，请放宽或更换检索词）"
        await db.arun(
            lambda: _update_query(
                query_id,
                status="failed",
                raw={
                    **base_raw,
                    "hits": [],
                    "dropped": dropped,
                    "searched": searched,
                    "skipped": skipped,
                    "failed": failed,
                    "failure_kind": kind,
                    "stderr": outcome.get("stderr") or "",
                },
                error=error,
            )
        )
        await _notify(on_progress, "failed", error)
        return SearchResult(
            status="failed",
            hits=[],
            error=error,
            query_id=query_id,
            terms=norm_terms,
            patent_type=ptype,
            elapsed_ms=_elapsed(),
            searched_terms=searched,
            skipped_terms=skipped,
            failed_terms=failed,
            failure_kind=kind,
        )

    rows = await db.arun(_insert_hits, case_id, query_id, hits)
    await db.arun(
        lambda: _update_query(
            query_id,
            status="done",
            raw={
                **base_raw,
                "hits": hits,
                "dropped": dropped,
                "searched": searched,
                "skipped": skipped,
                "failed": failed,
                "trimmed": trimmed,
                "type_gaps": type_gaps,
                # 部分完成的会话不能当完整结果进缓存（见 _cached_query）
                "partial": partial or bool(type_gaps),
            },
        )
    )
    gap_note = (
        "；" + "；".join(f"「{term}」的{'、'.join(gaps)}未检成" for term, gaps in type_gaps.items())
        if type_gaps
        else ""
    )
    await _notify(
        on_progress,
        "done",
        f"检索完成，入库 {len(rows)} 条" + (f"；{pending_note}" if partial else "") + gap_note,
    )
    return SearchResult(
        status="done",
        hits=[hit_row_to_model(r) for r in rows],
        error=None,
        query_id=query_id,
        terms=norm_terms,
        patent_type=ptype,
        elapsed_ms=_elapsed(),
        searched_terms=searched,
        skipped_terms=skipped,
        failed_terms=failed,
        gap_terms=[t for t, gaps in type_gaps.items() if gaps],
    )


async def _reuse_cached(
    case_id: str,
    cached: sqlite3.Row,
    norm_terms: Sequence[str],
    ptype: str,
    key: str,
) -> SearchResult | None:
    """复用缓存会话：同案件直接返回旧命中，跨案件复制一份进本案。"""
    if cached["case_id"] == case_id:
        rows = await db.arun(_hits_of_query, cached["id"])
        if not rows:
            return None
        return SearchResult(
            status="done",
            hits=[hit_row_to_model(r) for r in rows],
            query_id=cached["id"],
            terms=list(norm_terms),
            patent_type=ptype,
            cached=True,
            searched_terms=list(norm_terms),
        )

    try:
        raw = json.loads(cached["raw_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    hits = [h for h in (raw.get("hits") or []) if isinstance(h, Mapping)]
    if not hits:
        return None

    query_id = await db.arun(
        lambda: _insert_query(
            case_id,
            source="cnipa",
            patent_type=ptype,
            terms=norm_terms,
            status="done",
            raw={
                "terms_key": key,
                "terms": list(norm_terms),
                "patent_type": ptype,
                "hits": hits,
                "cached_from": cached["id"],
            },
        )
    )
    rows = await db.arun(_insert_hits, case_id, query_id, hits)
    return SearchResult(
        status="done",
        hits=[hit_row_to_model(r) for r in rows],
        query_id=query_id,
        terms=list(norm_terms),
        patent_type=ptype,
        cached=True,
        searched_terms=list(norm_terms),
    )


# ---------------------------------------------------------------------------
# 人工兜底 / 命中管理
# ---------------------------------------------------------------------------


def _coerce_manual(item: Any) -> dict[str, Any]:
    if isinstance(item, ManualHitIn):
        data = item.model_dump()
    elif isinstance(item, Mapping):
        data = dict(item)
    elif hasattr(item, "model_dump"):
        data = item.model_dump()
    else:  # pragma: no cover
        raise TypeError(f"无法解析的人工录入项：{type(item).__name__}")
    url = str(data.get("url") or "").strip()
    if not url:
        raise ValueError("人工录入的在先文献必须带可访问 URL（1.1 需附核验链接）")
    return {
        "url": url,
        "pub_no": data.get("pub_no") or None,
        "title": data.get("title") or None,
        "abstract": data.get("abstract") or None,
        "applicant": data.get("applicant") or None,
        "pub_date": data.get("pub_date") or None,
        "digest": data.get("digest") or None,
        "selected": bool(data.get("selected", True)),
    }


async def add_manual_hits(
    case_id: str, hits: Iterable[Any], *, note: str | None = None
) -> list[SearchHit]:
    """人工兜底录入在先文献（`manual_entry=1`）。

    A4 的失败门控里「用户粘贴在先文献」走这里；URL 必填（缺 URL 直接 ValueError，
    因为 1.1 要求每条附可核验链接）。同案件内 url 重复时复用旧行，不重复插。
    """
    items = [_coerce_manual(h) for h in hits or []]
    if not items:
        raise ValueError("人工录入清单为空")

    def op() -> list[sqlite3.Row]:
        query_id = _insert_query(
            case_id,
            source="manual",
            patent_type=None,
            terms=[],
            status="done",
            raw={"manual": True, "note": note or "", "count": len(items)},
        )
        return _insert_hits(case_id, query_id, items, manual=True)

    rows = await db.arun(op)
    return [hit_row_to_model(r) for r in rows]


# 相邻两篇补全之间的间隔：Google Patents 对密集请求会整站回 503
ENRICH_SPACING_SEC = 2.0


def _enrich_client() -> httpx.AsyncClient:
    """补全摘要用的 HTTP 客户端（单独成函数：测试里换成不触网的桩）。"""
    return httpx.AsyncClient(timeout=patent_fetch.FETCH_TIMEOUT, follow_redirects=True)


def _needs_abstract(hit: SearchHit) -> bool:
    return not str(hit.abstract or "").strip()


async def enrich_hits(
    case_id: str,
    hits: Sequence[SearchHit],
    *,
    on_progress: ProgressCallback | None = None,
    spacing: float | None = None,
) -> tuple[list[SearchHit], list[str]]:
    """给缺摘要的条目补上摘要 / 申请人 / 公开日（手工补录的通常只有标题和链接）。

    返回 `(按原顺序的条目, 未能补全的说明)`。已有摘要的条目原样返回、不发请求。
    **绝不抛异常**：补不上的如实列出原因，由调用方写进日志——不能让用户以为补过了。
    """
    targets = [h for h in hits if _needs_abstract(h)]
    if not targets:
        return list(hits), []
    gap = ENRICH_SPACING_SEC if spacing is None else spacing

    updated: dict[str, SearchHit] = {}
    failed: list[tuple[SearchHit, str, str, str]] = []      # (条目, 公开号, 标签, 原因)

    async def fill(hit: SearchHit, pub: str, info: Mapping[str, Any]) -> None:
        fields: dict[str, Any] = {"abstract": info["abstract"]}
        if not hit.applicant and info.get("applicant"):
            fields["applicant"] = info["applicant"]
        if not hit.pub_date and info.get("pub_date"):
            fields["pub_date"] = info["pub_date"]
        if not hit.title and info.get("title"):
            fields["title"] = info["title"]
        if not hit.pub_no and pub:
            fields["pub_no"] = pub
        try:
            updated[hit.id] = await _patch_hit(hit.id, fields)
        except KeyError:  # 内存态条目（无对应行）：只更新返回值
            updated[hit.id] = hit.model_copy(update=fields)

    # Google 整站限流（多见于代理出口 IP 被标记）时，退避重试救不回来：同一批里第一条
    # 退避完仍被限流，后面的条目就只试一次，尽快转 PDF 兜底，别每条都苦等十几秒
    retry_delays = patent_fetch.RATE_LIMIT_RETRY_DELAYS
    async with _enrich_client() as client:
        for n, hit in enumerate(targets):
            if n and gap > 0:
                await asyncio.sleep(gap)
            pub = patent_fetch.normalize_pub_no(hit.pub_no) or patent_fetch.pub_no_from_url(hit.url)
            url = str(hit.url or "")
            pdf_url = url if url.split("?", 1)[0].lower().endswith(".pdf") else None
            label = hit.title or pub or url
            await _notify(on_progress, "enrich", f"正在补全「{label}」的摘要（{n + 1}/{len(targets)}）…")
            summary = await patent_fetch.fetch_patent_summary(
                pub, pdf_url=pdf_url, client=client, retry_delays=retry_delays
            )
            if summary.rate_limited:
                retry_delays = ()
            if not summary.ok:
                failed.append((hit, pub, label, summary.error))
                continue
            await _notify(
                on_progress,
                "enrich",
                f"「{label}」摘要已补全（来源：{patent_fetch.SOURCE_LABELS.get(summary.source, summary.source)}）",
            )
            await fill(hit, pub, {"abstract": summary.abstract, "applicant": summary.applicant,
                                  "pub_date": summary.pub_date, "title": summary.title})

    # Google 拿不到、PDF 也没有的：到国知局公布公告取单行本的扉页图，OCR 认摘要。
    # 这是老文献（扫描件、站上无摘要）在 Google 不通时唯一的文字来源。
    rescue = [(h, pub, label, err) for h, pub, label, err in failed if pub]
    if rescue and await asyncio.to_thread(ocr_service.available):
        rescue = rescue[:MAX_FRONT_PAGE_PUBS]
        await _notify(
            on_progress, "enrich",
            f"Google 取不到，改从国知局公布公告取 {len(rescue)} 篇的扉页图识别摘要（每篇约 1 分钟）…",
        )
        pages = await fetch_front_pages([pub for _h, pub, _l, _e in rescue], on_progress=on_progress)
        for hit, pub, label, err in rescue:
            data = pages.get(pub) or {}
            info: dict[str, str] | None = None
            if data.get("path"):
                text = await asyncio.to_thread(ocr_service.image_text_upscaled, Path(str(data["path"])))
                info = patent_fetch.parse_front_page_text(text) if text else None
            if info and info["abstract"]:
                await _notify(on_progress, "enrich", f"「{label}」摘要已补全（来源：国知局单行本扉页 OCR）")
                await fill(hit, pub, {**info, "title": info["title"] or data.get("title") or "",
                                      "applicant": info["applicant"] or data.get("applicant") or "",
                                      "pub_date": info["pub_date"] or data.get("pub_date") or ""})
                failed = [f for f in failed if f[0].id != hit.id]
            else:
                why = data.get("error") or "扉页图 OCR 没有识别出摘要"
                failed = [(h, p, lb, f"{e}；国知局单行本：{why}" if h.id == hit.id else e) for h, p, lb, e in failed]

    problems = [f"{pub or label}：{err}" for _h, pub, label, err in failed]
    return [updated.get(h.id, h) for h in hits], problems


async def list_hits(case_id: str, *, selected_only: bool = False) -> list[SearchHit]:
    """案件的全部命中（按入库顺序）；`selected_only` 时只取被勾选的。"""
    sql = "SELECT * FROM search_hits WHERE case_id=?"
    if selected_only:
        sql += " AND selected=1"
    sql += " ORDER BY created_at ASC, rowid ASC"
    rows = await db.aquery_all(sql, (case_id,))
    return [hit_row_to_model(r) for r in rows]


async def set_selected(hit_id: str, selected: bool) -> SearchHit:
    """勾选/取消勾选一条命中（不存在抛 KeyError，API 层转 404）。"""
    return await _patch_hit(hit_id, {"selected": 1 if selected else 0})


async def set_digest(hit_id: str, digest: str) -> SearchHit:
    """回写 LLM 消化改写后的摘要（abstract_digest 阶段用）。"""
    return await _patch_hit(hit_id, {"digest": digest})


async def _patch_hit(hit_id: str, fields: Mapping[str, Any]) -> SearchHit:
    def op() -> sqlite3.Row:
        row = db.query_one("SELECT * FROM search_hits WHERE id=?", (hit_id,))
        if row is None:
            raise KeyError(f"检索命中不存在：{hit_id}")
        sets = ", ".join(f"{k}=?" for k in fields)
        db.execute(f"UPDATE search_hits SET {sets} WHERE id=?", (*fields.values(), hit_id))
        updated = db.query_one("SELECT * FROM search_hits WHERE id=?", (hit_id,))
        assert updated is not None
        return updated

    return hit_row_to_model(await db.arun(op))


async def hit_urls(case_id: str, *, selected_only: bool = True) -> set[str]:
    """命中 URL 白名单（1.1 写作 lint：产物中的 URL 必须 ∈ 本集合）。"""
    return {h.url for h in await list_hits(case_id, selected_only=selected_only)}


async def skip_search(case_id: str, reason: str = "") -> SearchQuery:
    """明确跳过查新：记一条 `manual_pending` 会话，1.1 须如实写明未检索。"""
    query_id = await db.arun(
        lambda: _insert_query(
            case_id,
            source="manual",
            patent_type=None,
            terms=[],
            status="manual_pending",
            raw={"skipped": True, "reason": reason or ""},
            error=None,
        )
    )
    row = await db.aquery_one("SELECT * FROM search_queries WHERE id=?", (query_id,))
    assert row is not None
    return _query_row_to_model(row, hit_count=0)


def _query_row_to_model(row: Mapping[str, Any] | sqlite3.Row, *, hit_count: int = 0) -> SearchQuery:
    """search_queries 行 → SearchQuery（terms_json / raw_json 解包）。"""
    d = dict(row)
    try:
        terms = json.loads(d.get("terms_json") or "[]")
    except (TypeError, json.JSONDecodeError):
        terms = []
    try:
        raw = json.loads(d.get("raw_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        raw = {}
    return SearchQuery(
        id=d["id"],
        case_id=d["case_id"],
        source=d["source"],
        patent_type=d.get("patent_type"),
        terms=[str(t) for t in terms] if isinstance(terms, list) else [],
        status=d["status"],
        error=d.get("error"),
        created_at=d.get("created_at") or "",
        hit_count=hit_count,
        cached=bool(raw.get("cached_from")),
        skipped=bool(raw.get("skipped")),
    )


async def list_queries(case_id: str) -> list[SearchQuery]:
    """案件的检索会话历史（新→旧），带各自命中数。"""
    rows = await db.aquery_all(
        "SELECT q.*, (SELECT COUNT(*) FROM search_hits h WHERE h.query_id=q.id) AS hit_count "
        "FROM search_queries q WHERE q.case_id=? ORDER BY q.created_at DESC, q.rowid DESC",
        (case_id,),
    )
    return [_query_row_to_model(r, hit_count=int(r["hit_count"] or 0)) for r in rows]


async def latest_query(case_id: str) -> SearchQuery | None:
    """最近一次检索会话（供门控判断是否已检索/失败/跳过）。"""
    queries = await list_queries(case_id)
    return queries[0] if queries else None


# ---------------------------------------------------------------------------
# 后台任务（API 触发时用；流水线内直接 await search()）
# ---------------------------------------------------------------------------

_tasks: dict[str, asyncio.Task] = {}


def is_searching(case_id: str) -> bool:
    """该案件是否有在跑的后台检索任务。"""
    task = _tasks.get(case_id)
    return task is not None and not task.done()


def start_background_search(
    case_id: str,
    terms: Iterable[str],
    patent_type: str = "invention",
    *,
    step_key: str | None = None,
    use_cache: bool = True,
    timeout: int | None = None,
) -> asyncio.Task:
    """启动后台检索任务（进度经 SSE `search_progress` 推送）；已在跑时抛 RuntimeError。"""
    if is_searching(case_id):
        raise RuntimeError("该案件的查新任务正在运行中")

    progress = hub_progress(case_id, step_key)

    async def _run() -> SearchResult:
        try:
            return await search(
                case_id,
                terms,
                patent_type,
                timeout=timeout,
                on_progress=progress,
                use_cache=use_cache,
            )
        finally:
            if _tasks.get(case_id) is asyncio.current_task():
                _tasks.pop(case_id, None)

    task = asyncio.create_task(_run(), name=f"cnipa-search:{case_id}")
    _tasks[case_id] = task
    return task
