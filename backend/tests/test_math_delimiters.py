# -*- coding: utf-8 -*-
"""LaTeX 原生分隔符归一化。

专利文书的写作规范要求「行内公式以 \\(...\\) 标记，独立公式以 \\[...\\] 标记」，
而转换器下游只认 ``$``/``$$``。这里锁住归一化的边界行为，
前端 `frontend/src/components/markdown/normalizeMath.ts` 是同一套规则。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "tools"))

from md_to_docx import _normalize_math_delimiters as norm  # noqa: E402


def test_oneline_display_becomes_block():
    """单行 \\[..\\] 拆成独占三行，才能命中下游的块级 $$ 分支。"""
    assert norm(r"\[ s_{p}=\alpha a_{p} \tag{1} \]").splitlines() == [
        "$$",
        r"s_{p}=\alpha a_{p} \tag{1}",
        "$$",
    ]


def test_multiline_display_delimiters():
    src = "\\[\na=b\n\\]"
    assert norm(src).splitlines() == ["$$", "a=b", "$$"]


def test_inline_paren():
    assert norm(r"取 \(\alpha\) 与 \(\beta\) 之和") == r"取 $\alpha$ 与 $\beta$ 之和"


def test_inline_paren_across_lines_left_alone():
    """跨行的 \\(..\\) 不处理——逐行匹配是有意的边界。

    正文里未闭合的 \\( 很常见（转义括号、流式渲染时的半截片段），
    放开跨行匹配会把一大段文字误吞成公式，代价远大于收益。
    """
    src = "值为 \\(a\n+\nb\\) 。"
    assert norm(src) == src


def test_fenced_code_untouched():
    src = "```text\n\\[ 不要动 \\]\n\\(x\\)\n```"
    assert norm(src) == src


def test_no_math_returns_input_unchanged():
    for src in ["普通文本，无公式。", "价格是 $100，也不该被动。", ""]:
        assert norm(src) == src


def test_unpaired_escape_left_alone():
    """孤立的 \\[ 是转义方括号，不成对就不当公式吞掉。"""
    src = r"数组下标 arr\[0\] 只是转义。"
    assert norm(src) == src


@pytest.mark.parametrize("src", [r"\[ a \]", r"\(a\)", "无公式"])
def test_idempotent(src):
    """归一化幂等：二次调用不再改动。"""
    once = norm(src)
    assert norm(once) == once
