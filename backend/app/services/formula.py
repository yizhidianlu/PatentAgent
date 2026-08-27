"""公式门禁（prompt-porting-spec §2 A6-G4 / R3）。

三件事：

1. **范式库**：装载 `assets/references/formulas/paradigms.yaml`（verbatim 资产），
   给出 **紧凑目录**（id + 名称 + 式形，供 prompt 注入）与 **全文查询**（选中后注入）；
2. **formula_plan 校验**：`validate_formula_plan()` = 移植脚本
   `tools/check_formula_plan.py` 的 `check_plan()` 校验 + `--eval` 数值复算
   （范式合法性、禁装饰音、numeric_example 可算性、化学/量纲启发式）；
3. **成文 lint**：`lint_latex()`（装饰音 / 上标写维度 / 分隔符混用 / 自由符号过多）与
   `symbols_consistency()`（3.4.1 符号表与 3.5 参数表逐字同形）。

### 与移植脚本的对接方式

`tools/check_formula_plan.py` 保留了原仓库的包内导入（`tools.shared.*`）与
`references/formulas/paradigms.yaml` 默认路径，两者在本项目的扁平 `app/tools/` 布局下
都不成立。本模块**不改动移植脚本**（保持可溯源），而是：

- 在 `sys.modules` 里注册 `tools` / `tools.shared` 两个指向 `app/tools/` 的命名空间包，
  使脚本的原样 import 生效（`_ensure_tools_package`）；
- 用脚本自带的环境变量扩展点 `PATENT_FORMULA_PARADIGMS` 指向本项目的资产路径。

调用层级：**进程内 import（首选）→ 子进程（同样带 shim 引导）→ degraded**。
degraded 时 `ok=False` 且 `degraded=True`，调用方据此区分「公式写错」与「门禁不可用」，
不要对后者跑 REPAIR 循环。
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import types
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

import anyio.to_thread

from .assets_loader import ASSETS_DIR
from .convert import TOOLS_DIR, run_tool  # noqa: F401  (run_tool 备用于扩展)

logger = logging.getLogger(__name__)

# 范式库（verbatim 资产）
PARADIGMS_PATH = ASSETS_DIR / "references" / "formulas" / "paradigms.yaml"

# 子进程校验超时（秒）：纯计算，给足即可
CHECK_TIMEOUT = 60

# 缺省禁用装饰音（范式库 rules.forbid_accent_commands 缺失时的兜底）
DEFAULT_FORBIDDEN_ACCENTS = (
    r"\tilde", r"\hat", r"\bar", r"\breve", r"\vec", r"\check", r"\grave", r"\acute",
)

# 缺省自由符号上限（范式库 rules.max_free_symbols）
DEFAULT_MAX_FREE_SYMBOLS = 12


class FormulaGateUnavailable(RuntimeError):
    """公式门禁不可用（移植脚本无法 import 也无法子进程执行）。"""


# ---------------------------------------------------------------------------
# 移植脚本接入（sys.modules shim + 资产路径环境变量）
# ---------------------------------------------------------------------------


def _ensure_paradigm_env() -> None:
    """让移植脚本的默认范式路径指向本项目资产（用户显式设置的环境变量优先）。"""
    if not os.environ.get("PATENT_FORMULA_PARADIGMS", "").strip():
        os.environ["PATENT_FORMULA_PARADIGMS"] = str(PARADIGMS_PATH)


def _ensure_tools_package() -> None:
    """注册 `tools` / `tools.shared` 命名空间包，指向扁平的 `app/tools/`。"""
    if isinstance(sys.modules.get("tools.shared"), types.ModuleType) and hasattr(
        sys.modules["tools.shared"], "__path__"
    ):
        return
    pkg = sys.modules.get("tools")
    if pkg is None or not hasattr(pkg, "__path__"):
        pkg = types.ModuleType("tools")
        pkg.__path__ = [str(TOOLS_DIR)]  # type: ignore[attr-defined]
        sys.modules["tools"] = pkg
    shared = types.ModuleType("tools.shared")
    shared.__path__ = [str(TOOLS_DIR)]  # type: ignore[attr-defined]
    sys.modules["tools.shared"] = shared
    pkg.shared = shared  # type: ignore[attr-defined]
    # 脚本内部还有同目录直接 import（如 `import stdio_utf8`）
    tools_dir = str(TOOLS_DIR)
    if tools_dir not in sys.path:
        sys.path.append(tools_dir)


def _import_checker():
    """import 移植的 check_formula_plan 模块；失败抛 ImportError。"""
    _ensure_paradigm_env()
    _ensure_tools_package()
    import tools.shared.check_formula_plan as checker  # type: ignore[import-not-found]

    return checker


def _import_paradigms():
    """import 移植的 formula_paradigms 模块；失败抛 ImportError。"""
    _ensure_paradigm_env()
    _ensure_tools_package()
    import tools.shared.formula_paradigms as mod  # type: ignore[import-not-found]

    return mod


# ---------------------------------------------------------------------------
# 一、范式库
# ---------------------------------------------------------------------------


def _load_yaml(path: Path) -> dict[str, Any]:
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise TypeError(f"范式库根须为 mapping：{path}")
    return data


@lru_cache(maxsize=8)
def load_paradigms(case_dir: str | None = None) -> dict[str, Any]:
    """装载合并后的范式库配置：`{version, rules, paradigms[], combos[], _sources[]}`。

    `case_dir` 非空时叠加案件目录下的 `formula_paradigms.yaml|json` 覆盖（同 id 后者胜）。
    移植脚本不可用时退化为直接读取资产 YAML（此时不做案件级覆盖合并之外的处理）。
    """
    try:
        mod = _import_paradigms()
        return mod.load_paradigms(case_dir)
    except Exception as exc:  # noqa: BLE001 —— 移植脚本不可用时退化为直读资产
        logger.warning("范式库走直读兜底（移植脚本不可用）：%s", exc)
        cfg = _load_yaml(PARADIGMS_PATH)
        cfg.setdefault("rules", {})
        cfg.setdefault("paradigms", [])
        cfg.setdefault("combos", [])
        cfg["_sources"] = [str(PARADIGMS_PATH)]
        return cfg


def rules(case_dir: str | None = None) -> dict[str, Any]:
    """范式库的硬性规则段（禁装饰音清单、自由符号上限、是否强制数值例等）。"""
    return dict(load_paradigms(case_dir).get("rules") or {})


def paradigm_catalog(case_dir: str | None = None) -> list[dict[str, Any]]:
    """**紧凑目录**：`[{id, name_zh, when_zh, tags, latex}]`，供 STRUCT 调用注入选题。

    `latex` 取 `render_latex`（成文体例，如 `x^{\\mathrm{norm}}`）优先，其次 `latex`
    —— 注入的永远是**可直接照抄的成文式形**。
    """
    out: list[dict[str, Any]] = []
    for p in load_paradigms(case_dir).get("paradigms") or []:
        if not isinstance(p, Mapping):
            continue
        out.append(
            {
                "id": str(p.get("id") or ""),
                "name_zh": str(p.get("name_zh") or ""),
                "when_zh": str(p.get("when_zh") or ""),
                "tags": list(p.get("tags") or []),
                "latex": str(p.get("render_latex") or p.get("latex") or "").strip(),
            }
        )
    return out


def catalog_summary(case_dir: str | None = None, *, with_when: bool = True) -> str:
    """紧凑目录的文本形式（注入 prompt 用；一行一条）。"""
    lines: list[str] = []
    for item in paradigm_catalog(case_dir):
        head = f"- {item['id']}｜{item['name_zh']}"
        if with_when and item["when_zh"]:
            head += f"｜适用：{item['when_zh']}"
        if item["latex"]:
            head += f"｜式形：{item['latex']}"
        lines.append(head)
    return "\n".join(lines)


def get_paradigm(paradigm_id: str, case_dir: str | None = None) -> dict[str, Any] | None:
    """按 id 取范式**全文**（选中后注入；含 notes_zh 等全部字段）。"""
    for p in load_paradigms(case_dir).get("paradigms") or []:
        if isinstance(p, Mapping) and str(p.get("id")) == str(paradigm_id):
            return dict(p)
    return None


def get_combo(combo_id: str, case_dir: str | None = None) -> dict[str, Any] | None:
    """按 id 取组合建议（`combos[]`）。"""
    for c in load_paradigms(case_dir).get("combos") or []:
        if isinstance(c, Mapping) and str(c.get("id")) == str(combo_id):
            return dict(c)
    return None


def list_combos(case_dir: str | None = None) -> list[dict[str, Any]]:
    """全部组合建议。"""
    return [dict(c) for c in load_paradigms(case_dir).get("combos") or [] if isinstance(c, Mapping)]


def paradigm_ids(case_dir: str | None = None) -> list[str]:
    """全部合法范式 id（白名单，禁止自创未登记范式）。"""
    return [str(p.get("id")) for p in load_paradigms(case_dir).get("paradigms") or [] if p.get("id")]


# ---------------------------------------------------------------------------
# 二、formula_plan 校验（check_plan + --eval 数值复算）
# ---------------------------------------------------------------------------


def _as_plan_dict(plan: Any) -> dict[str, Any]:
    """dict / pydantic FormulaPlan / 任意 model_dump 对象 → 纯 dict。"""
    if isinstance(plan, Mapping):
        return json.loads(json.dumps(dict(plan), ensure_ascii=False, default=str))
    if hasattr(plan, "model_dump"):
        return json.loads(plan.model_dump_json())
    raise TypeError(f"无法解析的 formula_plan 类型：{type(plan).__name__}")


_SUBPROCESS_BOOTSTRAP = r"""
import json, os, sys, types
tools_dir = sys.argv[1]
plan_path = sys.argv[2]
case_dir = sys.argv[3] or None
os.environ.setdefault("PATENT_FORMULA_PARADIGMS", sys.argv[4])
pkg = types.ModuleType("tools"); pkg.__path__ = [tools_dir]
shared = types.ModuleType("tools.shared"); shared.__path__ = [tools_dir]
sys.modules["tools"] = pkg; sys.modules["tools.shared"] = shared; pkg.shared = shared
sys.path.append(tools_dir)
import tools.shared.check_formula_plan as checker
plan = json.loads(open(plan_path, encoding="utf-8").read())
result = checker.check_plan(plan, case_dir=case_dir, eval_numeric=True)
sys.stdout.write("FORMULA_CHECK_JSON:" + json.dumps(result, ensure_ascii=False))
"""


def _check_plan_subprocess(plan: dict[str, Any], case_dir: str | None) -> dict[str, Any]:
    """子进程兜底：同样先装 shim 再调 `check_plan`，stdout 以 FORMULA_CHECK_JSON: 打头。"""
    from ..config import get_config

    cfg = get_config()
    cfg.ensure_dirs()
    tmp = cfg.tmp_dir / f"formula_plan_{os.getpid()}_{id(plan):x}.json"
    tmp.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        proc = subprocess.run(
            [
                sys.executable, "-c", _SUBPROCESS_BOOTSTRAP,
                str(TOOLS_DIR), str(tmp), case_dir or "", str(PARADIGMS_PATH),
            ],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(TOOLS_DIR),
            timeout=CHECK_TIMEOUT,
            check=False,
        )
    finally:
        tmp.unlink(missing_ok=True)

    marker = "FORMULA_CHECK_JSON:"
    for line in (proc.stdout or "").splitlines():
        if line.startswith(marker):
            return json.loads(line[len(marker):])
    detail = (proc.stderr or "").strip().splitlines()[-3:]
    raise FormulaGateUnavailable(f"子进程校验失败（exit={proc.returncode}）：{' | '.join(detail)}")


def validate_formula_plan(
    plan: Any,
    *,
    case_dir: str | None = None,
    eval_numeric: bool = True,
) -> dict[str, Any]:
    """校验 formula_plan（G4 门禁）。

    返回
    ----
    ```
    {
      "ok":        bool,                # 无 errors 且门禁可用
      "errors":    [str],               # 阻断项：未知范式 / 装饰音 / 数值例复算不符 / 缺数值例
      "warnings":  [str],               # 提示项：未列入 paradigm_ids、复杂式跳过代算、量纲启发式
      "evaluated": {                    # --eval 数值复算明细
          "equations": [{index, status: ok|mismatch|skip, reason?, lhs?, got?, expected?}],
          "ok": int, "mismatch": int, "skip": int, "any_ok": bool
      },
      "tags":      [str],               # 选中范式的标签并集（chemistry/score/...）
      "sources":   [str],               # 实际加载的范式库文件
      "mode":      "import"|"subprocess"|"unavailable",
      "degraded":  bool                 # True 表示门禁不可用（不要据此跑 REPAIR）
    }
    ```

    失败语义：`errors` 非空即 **G4 不通过**，调用方按 §2 A6 走 REPAIR≤2 → [PAUSE]。
    """
    plan_dict = _as_plan_dict(plan)

    result: dict[str, Any] | None = None
    mode = "import"
    try:
        checker = _import_checker()
        result = checker.check_plan(plan_dict, case_dir=case_dir, eval_numeric=eval_numeric)
    except Exception as exc:  # noqa: BLE001 —— 进程内不可用则退子进程
        logger.warning("公式门禁进程内调用失败，转子进程：%s", exc)
        try:
            result = _check_plan_subprocess(plan_dict, case_dir)
            mode = "subprocess"
        except Exception as sub_exc:  # noqa: BLE001
            logger.error("公式门禁不可用：%s", sub_exc)
            return {
                "ok": False,
                "errors": [f"公式门禁不可用（环境问题，非公式本身错误）：{sub_exc}"],
                "warnings": [],
                "evaluated": {"equations": [], "ok": 0, "mismatch": 0, "skip": 0, "any_ok": False},
                "tags": [],
                "sources": [],
                "mode": "unavailable",
                "degraded": True,
            }

    details = list(result.get("eval") or [])
    counts = {"ok": 0, "mismatch": 0, "skip": 0}
    for d in details:
        status = str(d.get("status") or "skip")
        counts[status] = counts.get(status, 0) + 1

    return {
        "ok": bool(result.get("ok")),
        "errors": [str(e) for e in (result.get("errors") or [])],
        "warnings": [str(w) for w in (result.get("warnings") or [])],
        "evaluated": {
            "equations": details,
            "ok": counts.get("ok", 0),
            "mismatch": counts.get("mismatch", 0),
            "skip": counts.get("skip", 0),
            "any_ok": counts.get("ok", 0) > 0,
        },
        "tags": [str(t) for t in (result.get("tags") or [])],
        "sources": [str(s) for s in (result.get("sources") or [])],
        "mode": mode,
        "degraded": False,
    }


async def avalidate_formula_plan(
    plan: Any, *, case_dir: str | None = None, eval_numeric: bool = True
) -> dict[str, Any]:
    """`validate_formula_plan` 的异步包装（子进程兜底时不阻塞事件循环）。"""
    return await anyio.to_thread.run_sync(
        lambda: validate_formula_plan(plan, case_dir=case_dir, eval_numeric=eval_numeric)
    )


# ---------------------------------------------------------------------------
# 三、成文 LaTeX lint（R3 的 regex 束）
# ---------------------------------------------------------------------------

# 允许的上标（幂次/转置/星号/序数之类），其余多字母上标视为「上标写维度」
_ALLOWED_SUPERSCRIPTS = {"t", "top", "star", "th", "n", "k", "m", "p", "q", "2", "3"}

_SUPERSCRIPT_RE = re.compile(r"\^\s*\{\s*([A-Za-z][A-Za-z0-9]*)\s*\}|\^\s*([A-Za-z][A-Za-z0-9]+)")

# 行内/块级分隔符
_DOLLAR_RE = re.compile(r"(?<!\\)\$")
_PAREN_RE = re.compile(r"\\[\(\)\[\]]")

# LaTeX 片段（$…$ / $$…$$ / \(…\) / \[…\]）
_SEGMENT_RE = re.compile(
    r"\$\$(?P<dd>.+?)\$\$|(?<!\\)\$(?P<d>[^$\n]+?)(?<!\\)\$"
    r"|\\\((?P<p>.+?)\\\)|\\\[(?P<b>.+?)\\\]",
    re.DOTALL,
)

# 数学函数 / 常用命令名（不计入自由符号）
_FUNCTION_NAMES = {
    "min", "max", "log", "ln", "exp", "sin", "cos", "tan", "arg", "sum", "prod",
    "frac", "sqrt", "cdot", "times", "left", "right", "mathrm", "text", "big",
    "in", "forall", "exists", "leq", "geq", "neq", "le", "ge", "to", "quad",
    "begin", "end", "cases", "top", "star", "mid", "ldots", "cdots", "operatorname",
}

_GREEK_NAMES = {
    "alpha", "beta", "gamma", "delta", "epsilon", "varepsilon", "zeta", "eta",
    "theta", "iota", "kappa", "lambda", "mu", "nu", "xi", "pi", "rho", "sigma",
    "tau", "upsilon", "phi", "varphi", "chi", "psi", "omega", "Delta", "Sigma",
    "Omega", "Gamma", "Lambda", "Phi", "Psi", "Theta",
}


def latex_segments(text: str) -> list[tuple[int, int, str]]:
    """抽出正文中的 LaTeX 片段 `[(inner_start, inner_end, inner)]`（偏移为**片段内容**在
    原文中的位置，便于 lint 直接回填 span）；正文无分隔符时整段视为一条公式。"""
    src = text or ""
    segs: list[tuple[int, int, str]] = []
    for m in _SEGMENT_RE.finditer(src):
        for name in ("dd", "d", "p", "b"):
            if m.group(name) is not None:
                start, end = m.span(name)
                segs.append((start, end, m.group(name)))
                break
    if not segs and src.strip():
        segs.append((0, len(src), src))
    return segs


def extract_symbols(text: str) -> list[str]:
    """抽取 LaTeX 片段中的**自由符号**（基符号，去掉下标/装饰/文字命令）。

    - `s_{ij}` / `s_{ik}` 归并为 `s`；`A_{j,\\mathrm{cpu}}` 归并为 `A`；
    - `\\alpha` 等希腊字母按命令名计一个符号；
    - `\\mathrm{}` / `\\text{}` 内的文字、函数名（min/max/log…）不计。
    """
    found: list[str] = []
    for _s, _e, inner in latex_segments(text):
        blob = inner.replace("\\\\", "\\")
        blob = re.sub(r"\\(?:mathrm|text|operatorname|mathbf|mathit)\s*\{[^{}]*\}", " ", blob)
        # 先收希腊字母等命令
        for m in re.finditer(r"\\([A-Za-z]+)", blob):
            name = m.group(1)
            if name in _GREEK_NAMES and name not in found:
                found.append(name)
        blob = re.sub(r"\\[A-Za-z]+", " ", blob)          # 去掉其余命令
        blob = re.sub(r"_\s*\{[^{}]*\}|_\s*[A-Za-z0-9]", " ", blob)   # 去下标
        blob = re.sub(r"\^\s*\{[^{}]*\}|\^\s*[A-Za-z0-9]", " ", blob)  # 去上标
        for m in re.finditer(r"[A-Za-z]+", blob):
            token = m.group(0)
            if token.lower() in _FUNCTION_NAMES:
                continue
            for ch in token:                                # 相邻字母视为隐式乘法
                if ch not in found:
                    found.append(ch)
    return found


def lint_latex(text: str, *, case_dir: str | None = None) -> list[dict[str, Any]]:
    """成文 LaTeX 体例 lint，返回 `[{rule, msg, span}]`（空清单=通过）。

    规则（prompt-porting-spec R3 / §8.2 体例项）：

    - `accent` —— `\\tilde` / `\\hat` / `\\bar` 等装饰音（Word/PNG 易渲成怪符号），
      清单取自范式库 `rules.forbid_accent_commands`；
    - `superscript_dimension` —— 上标写维度/文字（如 `b_i^{cpu}`），须改为
      下标 + `\\mathrm{}`（`b_{i,\\mathrm{cpu}}`）；
    - `delimiter_mix` —— 全文混用 `$…$` 与 `\\(…\\)`/`\\[…\\]`，须二选一；
    - `too_many_symbols` —— 自由符号超过范式库 `rules.max_free_symbols`（缺省 12）。

    `span` 为 `[start, end)` 字符偏移（无精确位置时为 None）。
    """
    src = text or ""
    findings: list[dict[str, Any]] = []

    cfg_rules = rules(case_dir)
    accents = [str(x) for x in (cfg_rules.get("forbid_accent_commands") or [])]
    if not accents:
        accents = list(DEFAULT_FORBIDDEN_ACCENTS)
    accent_names = sorted({a.lstrip("\\") for a in accents if a.strip()}, key=len, reverse=True)
    if cfg_rules.get("forbid_accents", True) and accent_names:
        accent_re = re.compile(r"\\(" + "|".join(re.escape(n) for n in accent_names) + r")\b")
        for m in accent_re.finditer(src):
            findings.append(
                {
                    "rule": "accent",
                    "msg": f"禁用装饰音 \\{m.group(1)}：改用独立符号（如平滑量 A）或 ema_smooth 范式",
                    "span": [m.start(), m.end()],
                    "match": m.group(0),
                }
            )

    for offset, _seg_end, inner in latex_segments(src):
        for m in _SUPERSCRIPT_RE.finditer(inner):
            word = m.group(1) or m.group(2) or ""
            if len(word) < 2 or word.lower() in _ALLOWED_SUPERSCRIPTS:
                continue
            findings.append(
                {
                    "rule": "superscript_dimension",
                    "msg": (
                        f"上标 ^{{{word}}} 疑似写维度/文字：须改为下标 + \\mathrm{{}}，"
                        f"如 b_{{i,\\mathrm{{{word}}}}}"
                    ),
                    "span": [offset + m.start(), offset + m.end()],
                    "match": m.group(0),
                }
            )

    dollars = list(_DOLLAR_RE.finditer(src))
    parens = list(_PAREN_RE.finditer(src))
    if dollars and parens:
        minority = parens if len(parens) <= len(dollars) else dollars
        first = minority[0]
        findings.append(
            {
                "rule": "delimiter_mix",
                "msg": (
                    f"行内/块级分隔符混用（$ 计 {len(dollars)} 处、\\( \\[ 计 {len(parens)} 处）："
                    "全文须统一为 $…$/$$…$$ 或 \\(…\\)/\\[…\\] 之一"
                ),
                "span": [first.start(), first.end()],
                "match": first.group(0),
            }
        )

    max_symbols = int(cfg_rules.get("max_free_symbols") or DEFAULT_MAX_FREE_SYMBOLS)
    symbols = extract_symbols(src)
    if max_symbols and len(symbols) > max_symbols:
        findings.append(
            {
                "rule": "too_many_symbols",
                "msg": (
                    f"自由符号 {len(symbols)} 个，超过上限 {max_symbols}："
                    f"{'、'.join(symbols[:16])}；请合并或改用向量/分式表述"
                ),
                "span": None,
                "symbols": symbols,
            }
        )

    return findings


# ---------------------------------------------------------------------------
# 四、符号跨节同形（3.4.1 符号表 ↔ 3.5 参数表）
# ---------------------------------------------------------------------------


def _symbol_text(item: Any) -> str:
    """符号项（str / dict / SymbolEntry）→ 符号文本。"""
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, Mapping):
        return str(item.get("symbol") or "").strip()
    return str(getattr(item, "symbol", "") or "").strip()


def _normalize_symbol(symbol: str) -> str:
    """近似同形归一：去 `$`、空白、`\\mathrm{}`/`\\text{}` 包裹、花括号与反斜杠。"""
    s = (symbol or "").strip().strip("$")
    s = re.sub(r"\\(?:mathrm|text|operatorname)\s*\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\(?:left|right|big|Big)\s*", "", s)
    s = s.replace("{", "").replace("}", "").replace("\\", "")
    return re.sub(r"\s+", "", s).lower()


def symbols_consistency(
    section_a_symbols: Iterable[Any],
    section_b_symbols: Iterable[Any],
    *,
    label_a: str = "3.4.1 符号表",
    label_b: str = "3.5 参数表",
) -> dict[str, Any]:
    """两节符号表的**逐字同形**差异报告（§8.2「3.5 与符号表逐字同形」）。

    入参可为 `list[str]`、`list[dict{symbol,…}]` 或 `list[SymbolEntry]` 混装。

    返回
    ----
    ```
    {
      "ok":        bool,                     # 无 only_in_a / only_in_b / near_miss
      "only_in_a": [str],                    # A 有 B 无
      "only_in_b": [str],                    # B 有 A 无
      "near_miss": [{"a","b","normalized"}], # 归一化后相同但**不逐字同形**（须统一写法）
      "common":    [str],
      "report":    [str]                     # 人类可读差异说明（可直接喂 REPAIR）
    }
    ```
    """
    a_list = [s for s in (_symbol_text(x) for x in section_a_symbols) if s]
    b_list = [s for s in (_symbol_text(x) for x in section_b_symbols) if s]
    a_seen, b_seen = list(dict.fromkeys(a_list)), list(dict.fromkeys(b_list))
    a_set, b_set = set(a_seen), set(b_seen)

    only_a = [s for s in a_seen if s not in b_set]
    only_b = [s for s in b_seen if s not in a_set]
    common = [s for s in a_seen if s in b_set]

    near: list[dict[str, str]] = []
    b_norm: dict[str, str] = {}
    for s in only_b:
        b_norm.setdefault(_normalize_symbol(s), s)
    matched_b: set[str] = set()
    for s in list(only_a):
        key = _normalize_symbol(s)
        hit = b_norm.get(key)
        if hit and hit not in matched_b:
            near.append({"a": s, "b": hit, "normalized": key})
            matched_b.add(hit)

    near_a = {x["a"] for x in near}
    near_b = {x["b"] for x in near}
    only_a = [s for s in only_a if s not in near_a]
    only_b = [s for s in only_b if s not in near_b]

    report: list[str] = []
    for x in near:
        report.append(f"符号写法不一致：{label_a}「{x['a']}」 vs {label_b}「{x['b']}」——须逐字同形")
    if only_a:
        report.append(f"{label_a}有而{label_b}缺：{'、'.join(only_a)}")
    if only_b:
        report.append(f"{label_b}有而{label_a}缺（须先在符号表定义）：{'、'.join(only_b)}")

    return {
        "ok": not (only_a or only_b or near),
        "only_in_a": only_a,
        "only_in_b": only_b,
        "near_miss": near,
        "common": common,
        "report": report,
    }


def plan_symbols(plan: Any) -> list[str]:
    """从 formula_plan 取符号表文本清单（供 `symbols_consistency` 的 A 侧）。"""
    data = _as_plan_dict(plan)
    return [s for s in (_symbol_text(x) for x in (data.get("symbols") or [])) if s]
