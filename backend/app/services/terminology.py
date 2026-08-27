"""术语表贯穿机制（prompt-porting-spec §0.3 / §3.1）。

三件事：

1. **术语表数据结构**：`TerminologySheet`（case_title / title_core_terms /
   term_family[{canonical, definition_zh, aliases, forbidden_variants, source_section}] /
   symbols），随 case 持久化并注入所有 GEN 调用（注入格式见 `to_prompt_dict`，
   字段名与 `assets/prompts/common/terminology_rules.md` 约定的
   `{terms:[{term, definition, aliases, forbidden_variants, source_section}], title_core_terms[]}`
   一致）。数据模型定义在 `models/disclosure.py`，此处 re-export，两处 import 皆可；
2. **terms_delta 剥离与合并**：每次 GEN 调用要求在文末输出一个 ```json 围栏块，
   服务端 `extract_terms_delta()` 剥离（**不进正文、不展示给用户**）后
   `merge_terms_delta()` 合并进术语表；
3. **漂移 lint**：`drift_lint()` 检查标题实词出现率、forbidden_variants 命中、
   别名顶替规范词 —— 命中即由调用方触发该章 REPAIR（§3.1）。

本模块纯函数：不碰 DB、不发 SSE、不调 LLM。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable, Mapping

from ..models.disclosure import (
    Skeleton,
    SymbolEntry,
    TermEntry,
    TerminologySheet,
    TermsDelta,
)

logger = logging.getLogger(__name__)

__all__ = [
    "SymbolEntry",
    "TermEntry",
    "TerminologySheet",
    "TermsDelta",
    "drift_lint",
    "extract_terms_delta",
    "extract_title_terms",
    "merge_terms_delta",
    "new_sheet",
    "skeleton_title_lint",
    "to_prompt_dict",
    "to_prompt_json",
]

# 标题套话（提取领域实词时剔除；「一种…方法及系统」之类）
_TITLE_STOPWORDS = (
    "一种", "一款", "一类", "基于", "用于", "及其", "以及", "并且", "或者",
    "方法", "系统", "装置", "设备", "终端", "平台", "介质", "产品", "程序",
    "存储介质", "电子设备", "计算机", "的", "及", "与", "和", "其",
)

# 标题切分标点
_TITLE_SPLIT_RE = re.compile(r"[\s，,、。．.；;：:（）()【】\[\]/|—\-]+")


# ---------------------------------------------------------------------------
# 构造与注入
# ---------------------------------------------------------------------------


def new_sheet(
    case_title: str = "",
    title_core_terms: Iterable[str] | None = None,
    terms: Iterable[Any] | None = None,
) -> TerminologySheet:
    """新建术语表；`title_core_terms` 缺省时从标题启发式提取领域实词。"""
    core = [str(t).strip() for t in (title_core_terms or []) if str(t).strip()]
    if not core and case_title:
        core = extract_title_terms(case_title)
    return TerminologySheet(
        case_title=case_title or "",
        title_core_terms=core,
        term_family=[_as_term(t) for t in (terms or [])],
    )


def extract_title_terms(case_title: str) -> list[str]:
    """从案件名称启发式提取领域实词（忽略「一种/基于/方法及系统」等套话）。

    仅作缺省兜底：G0 骨架的 `title_core_terms` 由模型给出时以模型结果为准。
    """
    title = (case_title or "").strip()
    if not title:
        return []
    # 用占位符切开套话，剩下的连续片段即候选实词
    marked = title
    for word in sorted(_TITLE_STOPWORDS, key=len, reverse=True):
        marked = marked.replace(word, " ")
    parts = [p.strip() for p in _TITLE_SPLIT_RE.split(marked) if p.strip()]
    out: list[str] = []
    for p in parts:
        if len(p) >= 2 and p not in out:
            out.append(p)
    return out


def to_prompt_dict(sheet: TerminologySheet | Mapping[str, Any] | None) -> dict[str, Any]:
    """术语表 → 注入 prompt 的紧凑 dict（键名照 terminology_rules.md 约定）。"""
    s = _as_sheet(sheet)
    return {
        "case_title": s.case_title,
        "title_core_terms": list(s.title_core_terms),
        "terms": [
            {
                "term": t.canonical,
                "definition": t.definition_zh,
                "aliases": list(t.aliases),
                "forbidden_variants": list(t.forbidden_variants),
                "source_section": t.source_section,
            }
            for t in s.term_family
        ],
        "symbols": [
            {"symbol": x.symbol, "meaning": x.meaning_zh, "unit": x.unit_zh} for x in s.symbols
        ],
    }


def to_prompt_json(sheet: TerminologySheet | Mapping[str, Any] | None) -> str:
    """术语表 → 注入 prompt 的 JSON 文本（UTF-8，不转义中文）。"""
    return json.dumps(to_prompt_dict(sheet), ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# terms_delta 剥离
# ---------------------------------------------------------------------------

# 文末 ```json 围栏块（允许无语言标注；贪婪到最后一个 ``` 之前）
_FENCE_RE = re.compile(r"```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\r?\n(.*?)\r?\n?```", re.DOTALL)


def _parse_delta_payload(raw: str) -> dict[str, Any] | None:
    """把围栏块内容解析为 terms_delta 载荷；不是术语增量则返回 None。"""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("terms_delta"), dict):
        return data["terms_delta"]
    # 容错：模型直接吐 {"add": [...], "update": [...]}
    if any(k in data for k in ("add", "update")) and all(
        isinstance(data.get(k, []), list) for k in ("add", "update")
    ):
        return data
    return None


def extract_terms_delta(gen_text: str) -> tuple[str, TermsDelta]:
    """剥离 GEN 产物文末的 ```json terms_delta``` 围栏块。

    返回 `(clean_text, delta)`：

    - 只认**最后一个**围栏块，且其后仅剩空白 —— 正文中间的示例 JSON 不会被误吞；
    - 该块解析失败或不含 `terms_delta` / `add` / `update` 时原样返回，delta 为空；
    - `clean_text` 已去掉围栏块与尾部空行（正文不含该块，也不展示给用户）。
    """
    text = gen_text or ""
    matches = list(_FENCE_RE.finditer(text))
    for m in reversed(matches):
        if text[m.end():].strip():          # 该块之后还有正文 → 不是文末增量块
            break
        payload = _parse_delta_payload(m.group(2))
        if payload is None:
            break
        clean = (text[: m.start()]).rstrip()
        try:
            delta = TermsDelta.model_validate(payload)
        except Exception as exc:  # noqa: BLE001 —— 增量格式错不该阻断成文
            logger.warning("terms_delta 解析失败（已忽略该块）：%s", exc)
            return clean, TermsDelta()
        return clean, delta
    return text, TermsDelta()


# ---------------------------------------------------------------------------
# 合并
# ---------------------------------------------------------------------------


def _as_term(item: Any) -> TermEntry:
    if isinstance(item, TermEntry):
        return item
    if isinstance(item, Mapping):
        return TermEntry.model_validate(item)
    if hasattr(item, "model_dump"):
        return TermEntry.model_validate(item.model_dump())
    return TermEntry.model_validate({"canonical": str(item)})


def _as_symbol(item: Any) -> SymbolEntry:
    if isinstance(item, SymbolEntry):
        return item
    if isinstance(item, Mapping):
        return SymbolEntry.model_validate(item)
    if hasattr(item, "model_dump"):
        return SymbolEntry.model_validate(item.model_dump())
    return SymbolEntry.model_validate({"symbol": str(item)})


def _as_sheet(sheet: Any) -> TerminologySheet:
    if isinstance(sheet, TerminologySheet):
        return sheet
    if sheet is None:
        return TerminologySheet()
    if isinstance(sheet, Mapping):
        return TerminologySheet.model_validate(sheet)
    if hasattr(sheet, "model_dump"):
        return TerminologySheet.model_validate(sheet.model_dump())
    raise TypeError(f"无法解析的术语表类型：{type(sheet).__name__}")


def _as_delta(delta: Any) -> TermsDelta:
    if isinstance(delta, TermsDelta):
        return delta
    if delta is None:
        return TermsDelta()
    if isinstance(delta, Mapping):
        payload = delta.get("terms_delta") if isinstance(delta.get("terms_delta"), Mapping) else delta
        return TermsDelta.model_validate(payload)
    if hasattr(delta, "model_dump"):
        return TermsDelta.model_validate(delta.model_dump())
    raise TypeError(f"无法解析的 terms_delta 类型：{type(delta).__name__}")


def _union(base: Iterable[str], extra: Iterable[str]) -> list[str]:
    """有序去重合并（保留先来顺序）。"""
    out: list[str] = []
    for item in list(base) + list(extra):
        s = str(item).strip()
        if s and s not in out:
            out.append(s)
    return out


def _merge_one(existing: TermEntry, incoming: TermEntry) -> TermEntry:
    """同名术语合并：定义取新（非空才覆盖），别名/禁用变体取并集。"""
    return TermEntry(
        canonical=existing.canonical,
        definition_zh=incoming.definition_zh or existing.definition_zh,
        aliases=_union(existing.aliases, incoming.aliases),
        forbidden_variants=_union(existing.forbidden_variants, incoming.forbidden_variants),
        source_section=incoming.source_section or existing.source_section,
    )


def merge_terms_delta(
    sheet: TerminologySheet | Mapping[str, Any] | None,
    delta: TermsDelta | Mapping[str, Any] | None,
) -> TerminologySheet:
    """把一次 GEN 调用的术语增量合并进术语表，返回**新的**术语表（不改入参）。

    规则：
    - `add`：规范词不存在则追加；已存在按 `update` 语义合并（模型常把二者混用）；
    - `update`：已存在则合并（定义非空才覆盖，别名/禁用变体取并集）；不存在则追加；
    - `symbols`：按 `symbol` 逐字同形去重合并（3.4.1 符号表的单一事实源）；
    - 规范词被登记后，若它出现在别的术语的 `forbidden_variants` 中不做自动清理 ——
      冲突交给 `drift_lint` 报出来，由人/REPAIR 决断。
    """
    s = _as_sheet(sheet)
    d = _as_delta(delta)

    family: list[TermEntry] = [t.model_copy(deep=True) for t in s.term_family]
    index = {t.canonical: i for i, t in enumerate(family)}

    for item in list(d.add) + list(d.update):
        entry = _as_term(item)
        if not entry.canonical.strip():
            continue
        i = index.get(entry.canonical)
        if i is None:
            index[entry.canonical] = len(family)
            family.append(entry)
        else:
            family[i] = _merge_one(family[i], entry)

    symbols: list[SymbolEntry] = [x.model_copy(deep=True) for x in s.symbols]
    sym_index = {x.symbol: i for i, x in enumerate(symbols)}
    for item in d.symbols:
        sym = _as_symbol(item)
        if not sym.symbol.strip():
            continue
        i = sym_index.get(sym.symbol)
        if i is None:
            sym_index[sym.symbol] = len(symbols)
            symbols.append(sym)
        else:
            symbols[i] = SymbolEntry(
                symbol=sym.symbol,
                meaning_zh=sym.meaning_zh or symbols[i].meaning_zh,
                unit_zh=sym.unit_zh or symbols[i].unit_zh,
                section=sym.section or symbols[i].section,
            )

    return TerminologySheet(
        case_title=s.case_title,
        title_core_terms=list(s.title_core_terms),
        term_family=family,
        symbols=symbols,
    )


# ---------------------------------------------------------------------------
# 漂移 lint
# ---------------------------------------------------------------------------


def _line_of(text: str, pos: int) -> int:
    """字符偏移 → 1 基行号。"""
    return text.count("\n", 0, pos) + 1


def _find_all(text: str, needle: str, limit: int = 20) -> list[int]:
    """全部出现位置（最多 limit 个）。"""
    out: list[int] = []
    start = 0
    while len(out) < limit:
        p = text.find(needle, start)
        if p < 0:
            break
        out.append(p)
        start = p + len(needle)
    return out


def _snippet(text: str, pos: int, width: int = 24) -> str:
    lo = max(0, pos - width // 2)
    return " ".join(text[lo : lo + width].split())


def drift_lint(
    chapter_text: str,
    sheet: TerminologySheet | Mapping[str, Any] | None,
    *,
    check_title_terms: bool = True,
    min_title_hits: int = 1,
) -> list[dict[str, Any]]:
    """术语漂移检查，返回 `[{rule, term, line, ...}]`（空清单=通过）。

    规则：

    - `forbidden_variant` —— 术语族 `forbidden_variants` 中的写法出现在正文/图示标签里
      （§7.9：换叫法须整族替换，禁止旧称残留）；
    - `alias_instead_of_canonical` —— 只用了别名而规范词全章未出现（同一对象两种叫法）；
    - `title_term_missing` —— 案件名称的领域实词在本章一次都没出现（`check_title_terms`
      对 3.2/3.4/第六章等要求贯穿的章节开启；对 3.4.1 符号表等可关闭）；
    - `title_term_rare` —— 出现次数低于 `min_title_hits`（>1 时才可能触发）。

    每条 finding 均带 `line`（1 基；无位置时为 None）与人类可读 `msg`。
    """
    text = chapter_text or ""
    s = _as_sheet(sheet)
    findings: list[dict[str, Any]] = []

    for term in s.term_family:
        for variant in term.forbidden_variants:
            v = str(variant).strip()
            if not v:
                continue
            for pos in _find_all(text, v):
                findings.append(
                    {
                        "rule": "forbidden_variant",
                        "term": v,
                        "canonical": term.canonical,
                        "line": _line_of(text, pos),
                        "snippet": _snippet(text, pos),
                        "msg": f"出现禁用变体「{v}」，应统一为「{term.canonical}」",
                    }
                )

    for term in s.term_family:
        canonical = term.canonical.strip()
        if not canonical or canonical in text:
            continue
        for alias in term.aliases:
            a = str(alias).strip()
            if not a or a not in text:
                continue
            pos = text.find(a)
            findings.append(
                {
                    "rule": "alias_instead_of_canonical",
                    "term": a,
                    "canonical": canonical,
                    "line": _line_of(text, pos),
                    "snippet": _snippet(text, pos),
                    "msg": f"本章只出现别名「{a}」，未出现规范词「{canonical}」",
                }
            )
            break

    if check_title_terms:
        for raw in s.title_core_terms:
            term = str(raw).strip()
            if not term:
                continue
            hits = _find_all(text, term)
            if not hits:
                findings.append(
                    {
                        "rule": "title_term_missing",
                        "term": term,
                        "line": None,
                        "count": 0,
                        "msg": f"案件名称实词「{term}」在本章未出现（标题须贯穿框图/流程/实施例）",
                    }
                )
            elif len(hits) < min_title_hits:
                findings.append(
                    {
                        "rule": "title_term_rare",
                        "term": term,
                        "line": _line_of(text, hits[0]),
                        "count": len(hits),
                        "msg": f"案件名称实词「{term}」仅出现 {len(hits)} 次，低于要求的 {min_title_hits} 次",
                    }
                )

    return findings


# ---------------------------------------------------------------------------
# G0 骨架校验（§2 A6：标题实词≥1 出现于 modules 与 steps 标签）
# ---------------------------------------------------------------------------


def skeleton_title_lint(skeleton: Skeleton | Mapping[str, Any]) -> list[str]:
    """骨架标题贯穿校验，返回错误说明清单（空=通过）。

    要求：`title_core_terms` 中**至少 1 个**实词出现在 `modules` 里，且**至少 1 个**
    出现在 `steps[].label` 里 —— 不满足即 G0 需带错反馈重跑。
    """
    sk = skeleton if isinstance(skeleton, Skeleton) else Skeleton.model_validate(dict(skeleton))
    errors: list[str] = []

    core = [t.strip() for t in sk.title_core_terms if str(t).strip()]
    if not core:
        core = extract_title_terms(sk.case_title)
    if not core:
        errors.append("title_core_terms 为空，且无法从 case_title 提取领域实词")
        return errors

    modules_blob = " ".join(sk.modules)
    steps_blob = " ".join(f"{s.sid} {s.label}" for s in sk.steps)
    if not any(t in modules_blob for t in core):
        errors.append(f"modules 中未出现任何标题实词（{'、'.join(core[:5])}）")
    if not any(t in steps_blob for t in core):
        errors.append(f"steps 标签中未出现任何标题实词（{'、'.join(core[:5])}）")
    if not sk.case_title.strip():
        errors.append("case_title 为空")
    return errors
