"""claims_lint 确定性校验测试（R4）：禁用词/句号/其特征在于/引用基础/多引不引多引/名称一致。"""

from __future__ import annotations

import pytest

from app.services.claims_lint import FORBIDDEN_WORDS, lint_claims


def _rules(findings: list[dict], rule: str) -> list[dict]:
    return [f for f in findings if f["rule"] == rule]


# ---------------------------------------------------------------------------
# 全绿基准
# ---------------------------------------------------------------------------

CLEAN_CLAIMS = [
    "一种基于深度学习的图像分割方法，其特征在于，包括：采集原始图像；对所述原始图像进行预处理；将预处理后的图像输入分割网络，得到分割结果。",
    "根据权利要求1所述的基于深度学习的图像分割方法，其特征在于，所述预处理包括灰度化处理与归一化处理。",
    "根据权利要求1或2所述的基于深度学习的图像分割方法，其特征在于，所述分割网络为卷积神经网络。",
]
CLEAN_NAME = "一种基于深度学习的图像分割方法"


def test_clean_claims_no_findings() -> None:
    assert lint_claims(CLEAN_CLAIMS, CLEAN_NAME) == []


def test_forbidden_word_table_matches_vendor() -> None:
    """禁用词表与 vendor README Critical Rules 规则1 逐字一致（14 项全表）。"""
    expected = {"等", "大约", "可能", "也许", "例如", "比如", "优选", "可以",
                "不限于", "部分", "某些", "若干", "某种程度上", "基本"}
    assert set(FORBIDDEN_WORDS) == expected


# ---------------------------------------------------------------------------
# 禁用词：14 词逐一命中（warning、只标记）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("word", FORBIDDEN_WORDS)
def test_forbidden_word_flagged(word: str) -> None:
    claim = f"一种测试装置，其特征在于，包括外壳与{word}连接件。"
    findings = _rules(lint_claims([claim], "测试装置"), "forbidden_word")
    assert findings, f"禁用词「{word}」未被标记"
    hit = findings[0]
    assert hit["severity"] == "warning"          # 命中只标记不删
    assert word in hit["message"]
    s, e = hit["span"]
    assert claim[s:e] == word                    # span 精确指向原文


def test_plasma_not_false_positive() -> None:
    """「等离子」不得因含「等」误伤。"""
    claims = [
        "一种等离子体处理装置，其特征在于，包括腔体与等离子体发生器。",
        "根据权利要求1所述的等离子体处理装置，其特征在于，所述腔体内壁设有均等分布的电极。",
    ]
    assert _rules(lint_claims(claims, "一种等离子体处理装置"), "forbidden_word") == []


def test_whitelist_compounds_not_flagged() -> None:
    """等效/等于/相等 等技术词豁免。"""
    claim = "一种滤波电路，其特征在于，第一电阻与第二电阻的阻值相等，且等效电感等于设定值。"
    assert _rules(lint_claims([claim], ""), "forbidden_word") == []


def test_plasma_plus_trailing_deng_still_flagged() -> None:
    """白名单只豁免命中区间本身：白名单词之外的「等」仍要标记。"""
    claim = "一种等离子体装置，其特征在于，包括电极、腔体等部件。"
    findings = _rules(lint_claims([claim], ""), "forbidden_word")
    assert len(findings) == 1
    s, e = findings[0]["span"]
    assert claim[s:e] == "等" and s > claim.index("等离子")


# ---------------------------------------------------------------------------
# 句号规则
# ---------------------------------------------------------------------------

def test_period_missing() -> None:
    findings = _rules(lint_claims(["一种装置，其特征在于，包括主体"], ""), "period")
    assert len(findings) == 1 and findings[0]["severity"] == "error"
    assert "缺少结尾句号" in findings[0]["message"]


def test_period_multiple() -> None:
    claim = "一种装置，其特征在于，包括主体。还包括支架。"
    findings = _rules(lint_claims([claim], ""), "period")
    assert len(findings) == 1
    s, e = findings[0]["span"]
    assert claim[s:e] == "。" and s == claim.index("。")   # 指向第一个违规句号


def test_period_not_at_end() -> None:
    findings = _rules(lint_claims(["一种装置，其特征在于。包括主体"], ""), "period")
    assert len(findings) == 1 and "未在结尾" in findings[0]["message"]


def test_single_trailing_period_ok() -> None:
    assert _rules(lint_claims(["一种装置，其特征在于，包括主体。"], ""), "period") == []


# ---------------------------------------------------------------------------
# 其特征在于（仅独权强制）
# ---------------------------------------------------------------------------

def test_independent_missing_characterized() -> None:
    findings = _rules(lint_claims(["一种装置，包括主体。"], ""), "independent_characterized")
    assert len(findings) == 1 and findings[0]["severity"] == "error"


def test_dependent_not_required_characterized() -> None:
    claims = [
        "一种装置，其特征在于，包括主体。",
        "根据权利要求1所述的装置，还包括支架。",   # 从权缺「其特征在于」不在本规则范围
    ]
    assert _rules(lint_claims(claims, ""), "independent_characterized") == []


# ---------------------------------------------------------------------------
# 引用规则
# ---------------------------------------------------------------------------

def test_reference_ge_self() -> None:
    claims = [
        "一种装置，其特征在于，包括主体。",
        "根据权利要求3所述的装置，其特征在于，还包括支架。",   # 引用 3 ≥ 自身 2
    ]
    findings = _rules(lint_claims(claims, ""), "reference_number")
    assert len(findings) == 1 and findings[0]["claim_no"] == 2 and "3" in findings[0]["message"]


def test_reference_zero_invalid() -> None:
    claims = [
        "一种装置，其特征在于，包括主体。",
        "根据权利要求0所述的装置，其特征在于，还包括支架。",
    ]
    assert len(_rules(lint_claims(claims, ""), "reference_number")) == 1


def test_reference_style_warning() -> None:
    claims = [
        "一种装置，其特征在于，包括主体。",
        "如权利要求1所述的装置，其特征在于，还包括支架。",   # 未用「根据权利要求N所述」
    ]
    findings = _rules(lint_claims(claims, ""), "reference_style")
    assert len(findings) == 1 and findings[0]["severity"] == "warning"


# ---------------------------------------------------------------------------
# 多引不引多引
# ---------------------------------------------------------------------------

def test_multi_ref_of_multi_flagged() -> None:
    claims = [
        "一种装置，其特征在于，包括主体。",
        "根据权利要求1所述的装置，其特征在于，还包括支架。",
        "根据权利要求1或2所述的装置，其特征在于，还包括底座。",       # 多引（1或2）
        "根据权利要求2或3所述的装置，其特征在于，还包括滚轮。",       # 多引且引了多引项3 → 违规
    ]
    findings = _rules(lint_claims(claims, ""), "multi_ref_of_multi")
    assert len(findings) == 1
    assert findings[0]["claim_no"] == 4 and "3" in findings[0]["message"]


def test_multi_ref_of_singles_ok() -> None:
    claims = [
        "一种装置，其特征在于，包括主体。",
        "根据权利要求1所述的装置，其特征在于，还包括支架。",
        "根据权利要求1或2所述的装置，其特征在于，还包括底座。",   # 多引，但被引项均为单引/独权
    ]
    assert _rules(lint_claims(claims, ""), "multi_ref_of_multi") == []


def test_range_any_of_parsed_as_multi() -> None:
    """「权利要求1至3中任一项」按区间展开为多引；被引项均非多引时合法。"""
    claims = [
        "一种装置，其特征在于，包括主体。",
        "根据权利要求1所述的装置，其特征在于，还包括支架。",
        "根据权利要求2所述的装置，其特征在于，还包括底座。",
        "根据权利要求1至3中任一项所述的装置，其特征在于，还包括滚轮。",
    ]
    findings = lint_claims(claims, "")
    assert _rules(findings, "multi_ref_of_multi") == []
    assert _rules(findings, "reference_number") == []
    # 若再追加一个多引项引用第 4 项 → 违规
    claims5 = claims + ["根据权利要求2或4所述的装置，其特征在于，还包括把手。"]
    assert len(_rules(lint_claims(claims5, ""), "multi_ref_of_multi")) == 1


# ---------------------------------------------------------------------------
# 名称一致
# ---------------------------------------------------------------------------

def test_name_mismatch() -> None:
    findings = _rules(
        lint_claims(["一种图像分割装置，其特征在于，包括处理器。"], "一种图像识别装置"),
        "name_consistency",
    )
    assert len(findings) == 1 and findings[0]["severity"] == "error"


def test_name_match_with_and_without_prefix() -> None:
    claim = ["一种图像分割装置，其特征在于，包括处理器。"]
    assert _rules(lint_claims(claim, "一种图像分割装置"), "name_consistency") == []
    assert _rules(lint_claims(claim, "图像分割装置"), "name_consistency") == []


def test_claim1_not_starting_yizhong() -> None:
    findings = _rules(
        lint_claims(["本装置包括主体，其特征在于，设有支架。"], "装置"), "name_consistency"
    )
    assert len(findings) == 1 and "一种" in findings[0]["message"]


def test_name_skip_when_not_provided() -> None:
    assert _rules(lint_claims(["一种装置，其特征在于，包括主体。"], ""), "name_consistency") == []


def test_name_span_points_at_claim_text() -> None:
    claim = "一种图像分割装置，其特征在于，包括处理器。"
    findings = _rules(lint_claims([claim], "一种别的装置"), "name_consistency")
    s, e = findings[0]["span"]
    assert claim[s:e] == "图像分割装置"


# ---------------------------------------------------------------------------
# 编号前缀容忍
# ---------------------------------------------------------------------------

def test_leading_numbering_stripped() -> None:
    claims = [
        "1. 一种检测装置，其特征在于，包括传感器。",
        "2. 根据权利要求1所述的检测装置，其特征在于，所述传感器为温度传感器。",
    ]
    assert lint_claims(claims, "检测装置") == []


def test_findings_shape() -> None:
    """产出契约：{claim_no, rule, severity, message, span}。"""
    findings = lint_claims(["一种装置，包括大约五米的导线"], "装置")
    assert findings
    for f in findings:
        assert set(f) == {"claim_no", "rule", "severity", "message", "span"}
        assert f["claim_no"] == 1
        assert f["severity"] in ("error", "warning")
        assert f["span"] is None or (isinstance(f["span"], list) and len(f["span"]) == 2)
