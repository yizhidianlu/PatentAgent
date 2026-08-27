"""国知局（CNIPA）公布公告查新服务（prompt-porting-spec §2 A4 / R8）。

子进程调移植脚本 `app/tools/cnipa_epub_search.py`（Playwright，浏览器顺序
chrome → msedge → chromium，见 `tools/browser.py`），沿用其机读 stdout 协议：

    stdout 仅一行： ``EPUB_HITS_JSON:`` + JSON 数组（[{title, pub_number, link, abstract}]）
    stderr 为 ASCII 的 ``EPUB_MERGE:`` / ``EPUB_NOTE:`` / ``EPUB_HINT:`` 等

铁律：

- **URL 照抄**：`search_hits.url` 一律取条目的 `link` 字段，缺 link 的条目直接丢弃
  （宁可少一条，也不给下游拼一个能编造的 URL）；
- **失败即降级**：超时 / WAF / 退出码非零 / 无 EPUB_HITS_JSON / 零解析，全部返回
  `status='failed'` 并写库，**绝不抛异常、绝不阻塞流水线** —— 由 A4 的三选项门控
  （重试 / 用户粘贴在先文献 / 跳过并如实写明未检索）兜底；
- **6 小时缓存**：同 terms+type（归一化哈希）命中 6 小时内的成功会话即复用，
  减少对 CNIPA 的请求频次（WAF 缓解手段之一）。

本模块只做「子进程 + 落库 + 进度回调」，不调 LLM。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

from ulid import ULID

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
from .convert import run_tool
from .sse import hub

logger = logging.getLogger(__name__)

# 检索脚本 / 探测脚本
SEARCH_SCRIPT = "cnipa_epub_search.py"
BROWSER_SCRIPT = "browser.py"

# 单次检索会话超时（秒）：backend-architecture §6 风险 1
SEARCH_TIMEOUT = 180
# 浏览器探测超时（秒）：含冷启动
PROBE_TIMEOUT = 120
# 结果缓存有效期（小时）
CACHE_TTL_HOURS = 6

_HITS_MARKER = "EPUB_HITS_JSON:"

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
}

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
    """构造把进度推成 SSE `search_progress{stage,msg}` 的回调（流水线/API 共用）。"""

    async def _cb(stage: str, msg: str) -> None:
        await hub.emit(
            case_id, "search_progress", {"stage": stage, "msg": msg}, step_key=step_key
        )

    return _cb


# ---------------------------------------------------------------------------
# 检索词与类型归一化
# ---------------------------------------------------------------------------


def normalize_type(patent_type: str | None) -> str:
    """专利类型 → 脚本 `--type` 参数（未知一律 all，绝不炸）。"""
    raw = str(patent_type or "").strip().lower()
    return _TYPE_ALIASES.get(raw, _TYPE_ALIASES.get(str(patent_type or "").strip(), "all"))


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


def _run_search_script(terms: Sequence[str], patent_type: str, timeout: int) -> dict[str, Any]:
    """跑一次检索子进程，返回 `{ok, hits, error, stderr}`（同步；供线程池调用）。"""
    args = ["--type", patent_type, *terms]
    env = _browser_env()
    # 脚本内部 setdefault EPUB_WAF_MAX_WAIT_SEC=180；留出子进程收尾余量
    env["EPUB_WAF_MAX_WAIT_SEC"] = str(max(30, timeout - 20))
    try:
        proc = run_tool(SEARCH_SCRIPT, args, timeout=timeout, extra_env=env)
    except subprocess.TimeoutExpired:
        return {"ok": False, "hits": [], "error": f"检索超时（{timeout}s，疑似 WAF 拦截或网络不可达）", "stderr": ""}
    except OSError as exc:
        return {"ok": False, "hits": [], "error": f"无法启动检索脚本：{exc}", "stderr": ""}

    stderr = proc.stderr or ""
    hits = parse_hits_stdout(proc.stdout or "")
    if proc.returncode != 0:
        return {
            "ok": False,
            "hits": [],
            "error": f"检索脚本退出码 {proc.returncode}：{_stderr_tail(stderr) or '无输出'}",
            "stderr": stderr,
        }
    if hits is None:
        return {
            "ok": False,
            "hits": [],
            "error": f"未解析到 EPUB_HITS_JSON 输出：{_stderr_tail(stderr) or '无输出'}",
            "stderr": stderr,
        }
    return {"ok": True, "hits": hits, "error": None, "stderr": stderr}


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
    timeout: int = SEARCH_TIMEOUT,
    on_progress: ProgressCallback | None = None,
    use_cache: bool = True,
    cache_ttl_hours: int = CACHE_TTL_HOURS,
    empty_is_failure: bool = True,
) -> SearchResult:
    """跑一次国知局检索并落库，返回 `SearchResult`（**永不抛网络异常**）。

    参数
    ----
    terms            : 检索词（一次会话多词，脚本内共用一个浏览器；上限 8 个）。
    patent_type      : invention | utility_model | design | all（其它值一律 all）。
    timeout          : 子进程超时（秒），默认 180。
    on_progress      : `cb(stage, msg)`，stage ∈ start|cache|running|parsed|done|failed。
    use_cache        : 命中 6 小时内同 terms+type 的成功会话即复用。
    empty_is_failure : 零解析视为失败（默认 True，交人工兜底门控；A4 明确禁止编造检索结果）。

    失败（超时 / WAF / 退出码非零 / 无 EPUB_HITS_JSON / 零解析）时
    `status='failed'` 且 `error` 有值，`search_queries` 同步写 failed 行。
    """
    started = time.monotonic()
    norm_terms = normalize_terms(terms)
    ptype = normalize_type(patent_type)

    if not norm_terms:
        error = "检索词为空"
        await _notify(on_progress, "failed", error)
        return SearchResult(status="failed", hits=[], error=error, terms=[], patent_type=ptype)

    key = terms_key(norm_terms, ptype)
    await _notify(
        on_progress, "start", f"开始检索国知局公布公告：{'、'.join(norm_terms)}（类型 {ptype}）"
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
    await _notify(on_progress, "running", f"正在检索（最长 {timeout}s，复用本机浏览器过 WAF）…")

    outcome = await db.arun(_run_search_script, norm_terms, ptype, timeout)

    if not outcome["ok"]:
        error = str(outcome["error"])
        await db.arun(
            lambda: _update_query(
                query_id,
                status="failed",
                raw={
                    "terms_key": key,
                    "terms": norm_terms,
                    "patent_type": ptype,
                    "stderr": _stderr_tail(outcome.get("stderr") or ""),
                },
                error=error,
            )
        )
        await _notify(on_progress, "failed", f"检索失败：{error}")
        logger.warning("CNIPA 检索失败 case=%s：%s", case_id, error)
        return SearchResult(
            status="failed",
            hits=[],
            error=error,
            query_id=query_id,
            terms=norm_terms,
            patent_type=ptype,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    hits, dropped = normalize_hits(outcome["hits"])
    await _notify(on_progress, "parsed", f"解析到 {len(hits)} 条命中（丢弃无链接 {dropped} 条）")

    if not hits and empty_is_failure:
        error = "检索未解析到任何命中（可尝试放宽检索词或改用 --type all）"
        await db.arun(
            lambda: _update_query(
                query_id,
                status="failed",
                raw={
                    "terms_key": key,
                    "terms": norm_terms,
                    "patent_type": ptype,
                    "hits": [],
                    "dropped": dropped,
                    "stderr": _stderr_tail(outcome.get("stderr") or ""),
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
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    rows = await db.arun(_insert_hits, case_id, query_id, hits)
    await db.arun(
        lambda: _update_query(
            query_id,
            status="done",
            raw={
                "terms_key": key,
                "terms": norm_terms,
                "patent_type": ptype,
                "hits": hits,
                "dropped": dropped,
            },
        )
    )
    await _notify(on_progress, "done", f"检索完成，入库 {len(rows)} 条")
    return SearchResult(
        status="done",
        hits=[hit_row_to_model(r) for r in rows],
        error=None,
        query_id=query_id,
        terms=norm_terms,
        patent_type=ptype,
        elapsed_ms=int((time.monotonic() - started) * 1000),
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
    timeout: int = SEARCH_TIMEOUT,
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
