# -*- coding: utf-8 -*-
"""交付边界：机器契约块不得出现在 Word / PDF 里。

机主在导出的 Word 正文里看到了 `"forbidden_variants": []`、`"terms_delta"`——
那是平台内部的术语增量契约，本该在逐章生成时就被剥离。

逐章剥离（terminology.extract_terms_delta）只认「文末最后一块」，这是对的：
它要防的是误吞正文中间的示例 JSON。但模型不总把契约放在最末（尾巴后又补一句话、
或分两块吐），漏网的那块会一路进到交付物里。所以装配这一层再兜一道。

**交付物是要交到审查员手里的东西**——正文里出现内部字段，比任何排版瑕疵都严重。
但反过来，误删用户正文里合法的 JSON 示例更糟：丢的是内容，不只是难看。
所以只删「认得出是契约」的块。
"""
from __future__ import annotations

from app.services.stream_filter import strip_contract_blocks

DOC = "\n".join([
    "## 三、技术方案",
    "",
    "正文段落一。",
    "",
    "```json",
    '{"terms_delta": {"add": [{"term": "SPPF", "forbidden_variants": []}], "update": []}}',
    "```",
    "",
    "尾巴之后还有一句话。",
    "",
    "```mermaid",
    "flowchart TD",
    "  A[采集] --> B[检测]",
    "```",
    "",
    "```json",
    '{"threshold": 0.75, "window": 30}',
    "```",
    "",
])


def test_contract_block_is_removed_from_the_document() -> None:
    out = strip_contract_blocks(DOC)
    assert "terms_delta" not in out, "术语增量契约漏进了交付物"
    assert "forbidden_variants" not in out
    assert "正文段落一" in out, "正文必须完好"


def test_text_after_the_contract_block_survives() -> None:
    """尾巴后又补了一句话——这正是逐章剥离漏网的那条路径。"""
    assert "尾巴之后还有一句话" in strip_contract_blocks(DOC)


def test_legitimate_json_example_is_kept() -> None:
    """正文里合法的 JSON 示例必须原样保留：误删正文比留下契约更糟。"""
    out = strip_contract_blocks(DOC)
    assert '"threshold"' in out and '"window"' in out


def test_mermaid_is_untouched() -> None:
    out = strip_contract_blocks(DOC)
    assert "flowchart TD" in out and "A[采集]" in out


def test_claims_contract_is_also_recognised() -> None:
    """权利要求书那份契约（invention_name + claims）同样不得进交付物。"""
    doc = "\n".join([
        "## 权利要求书",
        "",
        "1. 一种方法，其特征在于……。",
        "",
        "```json",
        '{"invention_name": "一种方法", "claims": ["1. 一种方法，其特征在于……。"]}',
        "```",
        "",
    ])
    out = strip_contract_blocks(doc)
    assert "invention_name" not in out
    assert "其特征在于" in out


def test_document_without_fences_is_returned_verbatim() -> None:
    plain = "## 一、技术领域\n\n本发明涉及医学图像处理。\n"
    assert strip_contract_blocks(plain) == plain


def test_empty_and_none_are_safe() -> None:
    assert strip_contract_blocks("") == ""
    assert strip_contract_blocks(None) is None  # type: ignore[arg-type]
