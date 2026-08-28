# -*- coding: utf-8 -*-
"""尾巴消音器：机器契约 JSON 不进人看的通道，正文与 mermaid 一个字不少。

机主截图里的两种真实形态：
1. 权利要求书正文之后滚出整块 ```json 围栏（内容与正文完全重复）——那是给
   服务端解析的契约，按提示词约定就该在末尾出现，但不该被转发给人看；
2. 模型不守围栏约定时，裸 JSON（"aliases": [], "forbidden_variants": []…）
   混进段落中间，像是文档坏了。
"""
from __future__ import annotations

import random

from app.services.stream_filter import TailMuter


def run(text: str, chunk: int = 7) -> str:
    """按固定小块喂流，拼出「人看到的」部分。"""
    m = TailMuter()
    out = []
    for i in range(0, len(text), chunk):
        out.append(m.feed(text[i:i + chunk]))
    out.append(m.flush())
    return "".join(out)


CLAIMS = (
    "## 权利要求书\n\n"
    "1. 一种气管插管解剖结构检测方法，其特征在于……。\n"
    "2. 根据权利要求1所述的方法，其特征在于……。\n"
)
TAIL = '```json\n{"invention_name": "一种方法", "claims": ["1.……。", "2.……。"]}\n```\n'


def test_fenced_json_tail_is_muted() -> None:
    seen = run(CLAIMS + "\n" + TAIL)
    assert "权利要求书" in seen and "其特征在于" in seen, "正文一个字不能少"
    assert "invention_name" not in seen, "契约 JSON 漏进了人看的通道"
    assert "```" not in seen


def test_bare_json_tail_is_muted() -> None:
    """不守围栏约定的模型直接甩裸 JSON——同样挡下。"""
    seen = run(CLAIMS + '\n{"terms": [{"term": "骨干网络", "aliases": []}], "update": []}\n')
    assert "其特征在于" in seen
    assert "aliases" not in seen


def test_mermaid_fences_pass_through_untouched() -> None:
    """交底书章节的框图就流在 doc 通道里，误杀它比不修还糟。"""
    doc = (
        "### 3.2 模块框图\n\n"
        "```mermaid\nflowchart TD\n  A[采集] --> B[检测]\n```\n\n"
        "如图所示……\n"
    )
    assert run(doc) == doc


def test_json_like_line_inside_a_fence_is_not_muted() -> None:
    """围栏体内以 { 开头的行不受裸 JSON 规则牵连。"""
    doc = "```mermaid\nstateDiagram\n{ 起始 }\n```\n后续正文。\n"
    assert run(doc) == doc


def test_muting_is_terminal() -> None:
    """尾巴开始后即使又出现普通文本也不放行——按契约尾巴只在末尾。"""
    seen = run(CLAIMS + TAIL + "这句不该出现。\n")
    assert "这句不该出现" not in seen


def test_json_from_the_first_byte_mutes_everything() -> None:
    """模型把整个回答写成 JSON：宁可这一段 chat 为空，也不给用户看裸契约。"""
    assert run('{"invention_name": "一种方法", "claims": ["1.……。"]}\n') == ""


def test_partial_last_line_without_newline_still_shown() -> None:
    """最后一行没有换行符也要放出来（很多模型的收尾没有 \\n）。"""
    assert run("正文第一行\n最后一行没换行") == "正文第一行\n最后一行没换行"


def test_random_chunking_never_changes_the_outcome() -> None:
    """分片边界是网络决定的：任何切法都必须得到同一个结果。"""
    text = CLAIMS + "\n" + TAIL
    expect = run(text, chunk=7)
    rng = random.Random(42)
    for _ in range(20):
        m = TailMuter()
        out, i = [], 0
        while i < len(text):
            step = rng.randint(1, 23)
            out.append(m.feed(text[i:i + step]))
            i += step
        out.append(m.flush())
        assert "".join(out) == expect, "结果随分片边界变化——行缓冲有漏洞"
