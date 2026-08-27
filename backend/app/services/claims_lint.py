"""权利要求确定性校验（prompt-porting-spec.md R4 / paper2patent Critical Rules 机器可判部分）。

lint_claims(claims, invention_name) → [{claim_no, rule, severity, message, span}]

规则：
- forbidden_word：14 个禁用词（vendor paper2patent README Critical Rules 规则1 全表；
  设计文档习称「15 禁用词表」，以 README 原表为准）。CJK 无词界 → **命中只标记
  （severity=warning）不自动删**，由 REPAIR 调用判断，防「等离子」类误伤——内置
  技术词白名单（等离子/等效/相等…）直接豁免；
- period：每项权利要求全文句号（。）计数 = 1 且必须位于结尾；
- independent_characterized：独立权利要求必须含「其特征在于」；
- reference_number：从属权利要求引用的权利要求编号必须 ≥1 且 < 自身编号；
- reference_style：从属权利要求宜采用「根据权利要求N所述」表述（warning）；
- multi_ref_of_multi：多项从属权利要求（引用 ≥2 项，如「权利要求1或2」「权利要求
  1至3中任一项」）不得再被另一多项从属权利要求引用；
- name_consistency：权利要求1 的「一种X」须与 invention_name 逐字一致
  （invention_name 自带「一种」前缀时先剥离再比对）。

span 为该发现在**原始权利要求字符串**中的 [start, end) 偏移（无精确位置时为 None）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# 禁用词表（paper2patent README Critical Rules 规则1，逐字核对）
# ---------------------------------------------------------------------------

FORBIDDEN_WORDS: list[str] = [
    "某种程度上",
    "不限于",
    "大约",
    "可能",
    "也许",
    "例如",
    "比如",
    "优选",
    "可以",
    "部分",
    "某些",
    "若干",
    "基本",
    "等",
]

# 技术词白名单：命中区间完全落在这些词内的禁用词豁免（防「等离子」误伤）
_WHITELIST_PATTERNS: dict[str, re.Pattern[str]] = {
    "等": re.compile(
        r"等离子|等效|等式|等于|等级|等温|等压|等电位|等间隔|等距|等分|等腰|等高|均等|相等"
    ),
}

_FW_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")

# 「权利要求N[至M][、或和 …]」引用块
_REF_BLOCK_RE = re.compile(
    r"权利要求\s*"
    r"((?:[0-9０-９]+(?:\s*[至到\-—~～]\s*[0-9０-９]+)?)"
    r"(?:\s*(?:[、,，]|或者|或|和)\s*[0-9０-９]+(?:\s*[至到\-—~～]\s*[0-9０-９]+)?)*)"
)

# 权利要求条目自带编号前缀：「1.」「1、」「1．」「1:」
_LEADING_NO_RE = re.compile(r"^\s*[0-9０-９]{1,3}\s*[\.．、,，:：]\s*")

# 权1 名称：「一种X，/。/：」
_NAME_RE = re.compile(r"一种(.+?)(?=[，,。：:；;])")


@dataclass
class _Claim:
    no: int
    text: str                      # 原始字符串
    body: str                      # 剥离编号前缀后的正文
    body_offset: int               # body 在 text 中的起始偏移
    refs: list[tuple[int, tuple[int, int]]] = field(default_factory=list)  # (被引号, 原文span)
    is_dependent: bool = False
    is_multi: bool = False         # 多项从属（引用 ≥2 项）


def _finding(
    claim_no: int, rule: str, severity: str, message: str, span: tuple[int, int] | None
) -> dict[str, Any]:
    return {
        "claim_no": claim_no,
        "rule": rule,
        "severity": severity,
        "message": message,
        "span": list(span) if span is not None else None,
    }


def _parse_ref_numbers(block: str) -> list[int]:
    """解析引用块数字：支持顿号/逗号/或/和 分隔与 N至M / N-M 区间展开。"""
    block = block.translate(_FW_DIGITS)
    nums: list[int] = []
    for part in re.split(r"[、,，]|或者|或|和", block):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*[至到\-—~～]\s*(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            nums.extend(range(min(a, b), max(a, b) + 1))
        elif re.fullmatch(r"\d+", part):
            nums.append(int(part))
    return nums


def _parse_claim(no: int, raw: str) -> _Claim:
    text = raw or ""
    m = _LEADING_NO_RE.match(text)
    body_offset = m.end() if m else 0
    body = text[body_offset:]

    claim = _Claim(no=no, text=text, body=body, body_offset=body_offset)
    ref_nums: set[int] = set()
    for bm in _REF_BLOCK_RE.finditer(text):
        span = bm.span()
        for num in _parse_ref_numbers(bm.group(1)):
            claim.refs.append((num, span))
            ref_nums.add(num)
    claim.is_dependent = bool(claim.refs)
    claim.is_multi = len(ref_nums) > 1
    return claim


# ---------------------------------------------------------------------------
# 单项规则
# ---------------------------------------------------------------------------

def _check_forbidden(c: _Claim) -> list[dict[str, Any]]:
    findings = []
    for word in FORBIDDEN_WORDS:
        wl = _WHITELIST_PATTERNS.get(word)
        wl_spans = [m.span() for m in wl.finditer(c.text)] if wl else []
        for m in re.finditer(re.escape(word), c.text):
            s, e = m.span()
            if any(ws <= s and e <= we for ws, we in wl_spans):
                continue  # 落在技术词白名单内，豁免
            findings.append(
                _finding(
                    c.no,
                    "forbidden_word",
                    "warning",
                    f"权利要求{c.no}出现不确定用语「{word}」；仅标记不自动删除，请复核（CJK 无词界，可能为技术词一部分）",
                    (s, e),
                )
            )
    return findings


def _check_period(c: _Claim) -> list[dict[str, Any]]:
    t = c.text.rstrip()
    if not t:
        return [_finding(c.no, "period", "error", f"权利要求{c.no}为空", None)]
    positions = [m.start() for m in re.finditer("。", t)]
    if len(positions) == 0:
        return [_finding(c.no, "period", "error", f"权利要求{c.no}缺少结尾句号", None)]
    if len(positions) > 1:
        first_bad = positions[0] if positions[0] != len(t) - 1 else positions[1]
        return [
            _finding(
                c.no,
                "period",
                "error",
                f"权利要求{c.no}含 {len(positions)} 个句号；每项只允许结尾一个句号，内部应改用分号或逗号",
                (first_bad, first_bad + 1),
            )
        ]
    if positions[0] != len(t) - 1:
        return [
            _finding(
                c.no,
                "period",
                "error",
                f"权利要求{c.no}的句号未在结尾（其后仍有内容）",
                (positions[0], positions[0] + 1),
            )
        ]
    return []


def _check_characterized(c: _Claim) -> list[dict[str, Any]]:
    if "其特征在于" in c.text:
        return []
    return [
        _finding(
            c.no,
            "independent_characterized",
            "error",
            f"独立权利要求{c.no}缺少「其特征在于」",
            None,
        )
    ]


def _check_refs(c: _Claim) -> list[dict[str, Any]]:
    findings = []
    for num, span in c.refs:
        if num < 1 or num >= c.no:
            findings.append(
                _finding(
                    c.no,
                    "reference_number",
                    "error",
                    f"权利要求{c.no}引用了无效的权利要求编号 {num}（必须 ≥1 且小于自身编号）",
                    span,
                )
            )
    if "根据权利要求" not in c.body:
        findings.append(
            _finding(
                c.no,
                "reference_style",
                "warning",
                f"从属权利要求{c.no}宜采用「根据权利要求N所述」的引用表述",
                None,
            )
        )
    return findings


def _check_multi_of_multi(claims: list[_Claim]) -> list[dict[str, Any]]:
    findings = []
    by_no = {c.no: c for c in claims}
    for c in claims:
        if not c.is_multi:
            continue
        for num, span in c.refs:
            target = by_no.get(num)
            if target is not None and target.is_multi:
                findings.append(
                    _finding(
                        c.no,
                        "multi_ref_of_multi",
                        "error",
                        f"多项从属权利要求{c.no}引用了另一多项从属权利要求{num}（多引不得引多引）",
                        span,
                    )
                )
    return findings


def _check_name(claims: list[_Claim], invention_name: str) -> list[dict[str, Any]]:
    if not claims or not (invention_name or "").strip():
        return []
    c1 = claims[0]
    m = _NAME_RE.match(c1.body)
    if m is None:
        return [
            _finding(
                1,
                "name_consistency",
                "error",
                "权利要求1未以「一种…」开头，无法与发明名称核对",
                None,
            )
        ]
    expected = invention_name.strip()
    if expected.startswith("一种"):
        expected = expected[len("一种"):].strip()
    actual = m.group(1).strip()
    if actual != expected:
        span = (c1.body_offset + m.start(1), c1.body_offset + m.end(1))
        return [
            _finding(
                1,
                "name_consistency",
                "error",
                f"权利要求1的主题名称「一种{actual}」与发明名称「{invention_name.strip()}」不一致（须逐字一致）",
                span,
            )
        ]
    return []


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def lint_claims(claims: list[str], invention_name: str = "") -> list[dict[str, Any]]:
    """校验整套权利要求；返回按权利要求编号排序的发现列表（空列表 = 全部通过）。"""
    parsed = [_parse_claim(i, text) for i, text in enumerate(claims, 1)]
    findings: list[dict[str, Any]] = []
    for c in parsed:
        findings.extend(_check_forbidden(c))
        findings.extend(_check_period(c))
        if c.is_dependent:
            findings.extend(_check_refs(c))
        else:
            findings.extend(_check_characterized(c))
    findings.extend(_check_multi_of_multi(parsed))
    findings.extend(_check_name(parsed, invention_name))
    findings.sort(key=lambda f: (f["claim_no"], f["span"][0] if f["span"] else -1))
    return findings
