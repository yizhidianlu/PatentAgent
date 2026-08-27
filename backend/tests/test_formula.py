"""公式门禁测试（prompt-porting-spec §2 A6-G4 / R3）。

全部用**真实**范式库 `app/assets/references/formulas/paradigms.yaml`：
- 合法 formula_plan（范式 ∈ 白名单、数值例可代算）→ ok；
- 数值例复算不符 / 未知范式 / 装饰音 / 缺数值例 → 各自报错；
- lint_latex：装饰音、上标写维度、分隔符混用、自由符号超限；
  并验证范式推荐写法 `x^{\\mathrm{norm}}` **不**误报；
- symbols_consistency：3.4.1 与 3.5 的逐字同形差异；
- 子进程兜底路径（进程内 import 不可用时）产出与进程内一致。
"""

from __future__ import annotations

from app.models.disclosure import FormulaPlan
from app.services import formula

# 加权和：s_{ij} = 0.6 b + 0.4 g → 0.6*0.8 + 0.4*0.5 = 0.68
VALID_PLAN = {
    "theme_summary": "任务与节点的资源匹配打分",
    "paradigm_ids": ["weighted_sum", "dual_threshold"],
    "plain_zh": "匹配分 = 权重×CPU 适配 + 权重×内存适配",
    "equations": [
        {"tag": 1, "paradigm_id": "weighted_sum", "latex": "s_{ij} = 0.6 b + 0.4 g", "role": "score"},
        {
            "tag": 2,
            "paradigm_id": "dual_threshold",
            "latex": "\\Delta t \\ge T_r ; |\\sigma - \\sigma'| \\ge \\Delta s",
            "role": "trigger",
        },
    ],
    "symbols": [
        {"symbol": "s_{ij}", "meaning_zh": "任务i在节点j上的匹配分", "unit_zh": "无量纲"},
        {"symbol": "b", "meaning_zh": "任务的CPU需求权重", "unit_zh": "[0,1]"},
        {"symbol": "g", "meaning_zh": "节点的内存可用比例", "unit_zh": "[0,1]"},
    ],
    "numeric_example": {
        "given": {"b": 0.8, "g": 0.5},
        "compute_zh": "0.6×0.8 + 0.4×0.5 = 0.68",
        "result": {"s_{ij}": 0.68},
    },
}


def _plan(**overrides) -> dict:
    import copy

    plan = copy.deepcopy(VALID_PLAN)
    plan.update(overrides)
    return plan


# ---------------------------------------------------------------------------
# 一、范式库目录与全文
# ---------------------------------------------------------------------------


def test_paradigm_catalog_is_compact_and_complete():
    """紧凑目录：id + 名称 + 式形，条数与范式库一致。"""
    catalog = formula.paradigm_catalog()
    ids = [c["id"] for c in catalog]
    assert "weighted_sum" in ids
    assert "dual_threshold" in ids
    assert "stoichiometric_reaction" in ids
    assert len(catalog) == len(formula.load_paradigms().get("paradigms"))

    entry = next(c for c in catalog if c["id"] == "weighted_sum")
    assert entry["name_zh"] == "加权和打分"
    assert entry["latex"]
    assert set(entry) == {"id", "name_zh", "when_zh", "tags", "latex"}


def test_catalog_summary_one_line_per_paradigm():
    """注入用摘要：一行一条，含 id 与式形。"""
    text = formula.catalog_summary()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert len(lines) == len(formula.paradigm_catalog())
    assert any(ln.startswith("- weighted_sum｜") for ln in lines)
    assert "式形：" in text


def test_paradigm_full_lookup():
    """全文查询：含 notes_zh 等目录里没有的字段；未知 id 返回 None。"""
    p = formula.get_paradigm("weighted_sum_unit")
    assert p is not None
    assert p["render_latex"].startswith("s = ")
    assert "禁用" in p["notes_zh"]
    assert formula.get_paradigm("no_such_paradigm") is None

    combo = formula.get_combo("match_then_rate_limit")
    assert combo is not None
    assert "weighted_sum_unit" in combo["paradigm_ids"]
    assert "weighted_sum" in formula.paradigm_ids()


def test_rules_loaded_from_yaml():
    """规则段：禁装饰音清单与自由符号上限来自范式库。"""
    r = formula.rules()
    assert r["forbid_accents"] is True
    assert "\\tilde" in r["forbid_accent_commands"]
    assert r["max_free_symbols"] == 12


# ---------------------------------------------------------------------------
# 二、formula_plan 校验（含 --eval 数值复算）
# ---------------------------------------------------------------------------


def test_valid_plan_passes_with_numeric_eval():
    """合法计划：范式合法 + 数值例复算通过。"""
    result = formula.validate_formula_plan(VALID_PLAN)
    assert result["ok"] is True
    assert result["errors"] == []
    assert result["degraded"] is False
    assert result["mode"] == "import"
    assert result["evaluated"]["ok"] >= 1
    assert result["evaluated"]["any_ok"] is True
    assert result["evaluated"]["mismatch"] == 0
    # 打分类范式的 tags 并入报告，供化学/量纲分支判断
    assert "score" in result["tags"]
    assert result["sources"] and result["sources"][0].endswith("paradigms.yaml")


def test_numeric_mismatch_fails():
    """数值例复算不符 → 阻断（G4 不通过，走 REPAIR）。"""
    plan = _plan(
        numeric_example={
            "given": {"b": 0.8, "g": 0.5},
            "compute_zh": "算错了",
            "result": {"s_{ij}": 0.99},
        }
    )
    result = formula.validate_formula_plan(plan)
    assert result["ok"] is False
    assert any("代算" in e for e in result["errors"])
    assert result["evaluated"]["mismatch"] == 1
    assert result["degraded"] is False


def test_unknown_paradigm_fails():
    """自创未登记范式 → 阻断。"""
    plan = _plan(paradigm_ids=["weighted_sum", "my_secret_formula"])
    result = formula.validate_formula_plan(plan)
    assert result["ok"] is False
    assert any("my_secret_formula" in e for e in result["errors"])


def test_accent_in_equation_fails():
    """公式里出现 \\hat 等装饰音 → 阻断（Word/PNG 渲染怪符号）。"""
    plan = _plan(
        equations=[
            {
                "tag": 1,
                "paradigm_id": "weighted_sum",
                "latex": "s_{ij} = 0.6 \\hat{b} + 0.4 g",
                "role": "score",
            }
        ]
    )
    result = formula.validate_formula_plan(plan)
    assert result["ok"] is False
    assert any("装饰音" in e for e in result["errors"])


def test_missing_numeric_example_fails():
    """有公式却没有可算数值例 → 阻断（rules.require_numeric_example）。"""
    plan = _plan(numeric_example={})
    result = formula.validate_formula_plan(plan)
    assert result["ok"] is False
    assert any("numeric_example" in e for e in result["errors"])


def test_equation_paradigm_not_listed_is_warning_only():
    """equations 用了未列入 paradigm_ids 的合法范式 → 只警告，不阻断。"""
    plan = _plan(paradigm_ids=["weighted_sum"])
    result = formula.validate_formula_plan(plan)
    assert result["ok"] is True
    assert any("dual_threshold" in w for w in result["warnings"])


def test_pydantic_plan_accepted():
    """契约模型直通：FormulaPlan 可直接送门禁。"""
    result = formula.validate_formula_plan(FormulaPlan.model_validate(VALID_PLAN))
    assert result["ok"] is True
    assert formula.plan_symbols(FormulaPlan.model_validate(VALID_PLAN))[0] == "s_{ij}"


def test_subprocess_fallback_matches_inprocess(monkeypatch):
    """进程内 import 不可用时走子进程兜底，结论一致（mode 标记为 subprocess）。"""

    def _boom():
        raise ImportError("模拟移植脚本 import 失败")

    monkeypatch.setattr(formula, "_import_checker", _boom)
    result = formula.validate_formula_plan(VALID_PLAN)
    assert result["mode"] == "subprocess"
    assert result["ok"] is True
    assert result["degraded"] is False
    assert result["evaluated"]["ok"] >= 1

    bad = formula.validate_formula_plan(_plan(paradigm_ids=["nope"]))
    assert bad["mode"] == "subprocess"
    assert bad["ok"] is False


def test_gate_unavailable_is_flagged(monkeypatch):
    """门禁本身不可用时 degraded=True —— 调用方据此区分「环境坏」与「公式错」。"""

    def _boom(*_args, **_kwargs):
        raise ImportError("模拟不可用")

    monkeypatch.setattr(formula, "_import_checker", _boom)
    monkeypatch.setattr(formula, "_check_plan_subprocess", _boom)
    result = formula.validate_formula_plan(VALID_PLAN)
    assert result["ok"] is False
    assert result["degraded"] is True
    assert result["mode"] == "unavailable"


# ---------------------------------------------------------------------------
# 三、成文 LaTeX lint
# ---------------------------------------------------------------------------


def test_lint_accent_and_superscript_dimension():
    """装饰音 + 上标写维度：各报一条，span 指回原文位置。"""
    text = "匹配分为 $s = \\tilde{a} + b_i^{cpu}$，其中上标表示维度。"
    findings = formula.lint_latex(text)
    rules_hit = {f["rule"] for f in findings}
    assert "accent" in rules_hit
    assert "superscript_dimension" in rules_hit

    accent = next(f for f in findings if f["rule"] == "accent")
    assert text[accent["span"][0] : accent["span"][1]] == "\\tilde"
    sup = next(f for f in findings if f["rule"] == "superscript_dimension")
    assert text[sup["span"][0] : sup["span"][1]] == "^{cpu}"
    assert "\\mathrm" in sup["msg"]


def test_lint_allows_recommended_mathrm_superscript():
    """范式推荐写法 x^{\\mathrm{norm}} / r^{\\top} 不得误报。"""
    text = "$s = \\alpha x^{\\mathrm{norm}} + \\beta y^{\\mathrm{norm}}$ 与 $c = r^{\\top} v$"
    findings = formula.lint_latex(text)
    assert [f for f in findings if f["rule"] == "superscript_dimension"] == []
    assert [f for f in findings if f["rule"] == "accent"] == []


def test_lint_delimiter_mix():
    """$…$ 与 \\(…\\) 混用 → 报一条 delimiter_mix。"""
    findings = formula.lint_latex("行内 $a = b$，另一处 \\(c = d\\)。")
    mix = [f for f in findings if f["rule"] == "delimiter_mix"]
    assert len(mix) == 1
    assert "混用" in mix[0]["msg"]

    assert [f for f in formula.lint_latex("统一写法 $a = b$ 与 $c = d$") if f["rule"] == "delimiter_mix"] == []


def test_lint_too_many_free_symbols():
    """自由符号超过范式库上限（12）→ 报 too_many_symbols。"""
    text = "$y = a + b + c + d + e + f + g + h + i + j + k + l + m$"
    findings = formula.lint_latex(text)
    hit = [f for f in findings if f["rule"] == "too_many_symbols"]
    assert len(hit) == 1
    assert hit[0]["span"] is None
    assert len(hit[0]["symbols"]) > 12

    ok_text = "$s = \\alpha b + \\beta g$"
    assert [f for f in formula.lint_latex(ok_text) if f["rule"] == "too_many_symbols"] == []


def test_lint_clean_text_returns_empty():
    """合规公式：无 finding。"""
    assert formula.lint_latex("$s_{ij} = 0.6 b_{i,\\mathrm{cpu}} + 0.4 g_{j,\\mathrm{mem}}$") == []


def test_extract_symbols_merges_subscripts():
    """s_{ij} 与 s_{ik} 归并为同一自由符号 s；希腊字母按命令名计。"""
    symbols = formula.extract_symbols("$s_{ij} = \\alpha b_{i} + \\beta g_{j} - \\lambda n$")
    assert symbols.count("s") == 1
    assert "alpha" in symbols and "lambda" in symbols
    assert "mathrm" not in symbols


# ---------------------------------------------------------------------------
# 四、符号跨节同形（3.4.1 ↔ 3.5）
# ---------------------------------------------------------------------------


def test_symbols_consistency_identical():
    """逐字同形 → ok。"""
    report = formula.symbols_consistency(["s_{ij}", "T_r"], ["s_{ij}", "T_r"])
    assert report["ok"] is True
    assert report["report"] == []
    assert report["common"] == ["s_{ij}", "T_r"]


def test_symbols_consistency_near_miss_and_missing():
    """写法不一致（归一化后相同）与单边缺失分别报出。"""
    report = formula.symbols_consistency(
        [{"symbol": "A_{j,\\mathrm{cpu}}"}, {"symbol": "s_{ij}"}],
        ["A_{j,cpu}", "s_{ij}", "w"],
    )
    assert report["ok"] is False
    assert report["near_miss"] == [
        {"a": "A_{j,\\mathrm{cpu}}", "b": "A_{j,cpu}", "normalized": "a_j,cpu"}
    ]
    assert report["only_in_b"] == ["w"]
    assert report["only_in_a"] == []
    assert any("逐字同形" in line for line in report["report"])


def test_symbols_consistency_accepts_symbol_entries():
    """SymbolEntry（3.4.1）与纯字符串（3.5）混装可比。"""
    from app.models.disclosure import SymbolEntry

    report = formula.symbols_consistency(
        [SymbolEntry(symbol="s_{ij}", meaning_zh="匹配分"), SymbolEntry(symbol="T_r")],
        ["s_{ij}"],
    )
    assert report["only_in_a"] == ["T_r"]
    assert "3.4.1 符号表有而3.5 参数表缺" in report["report"][0]
