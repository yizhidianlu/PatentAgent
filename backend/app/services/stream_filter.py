# -*- coding: utf-8 -*-
"""流式转发的「尾巴消音器」：机器契约 JSON 不进人看的通道。

**为什么需要它。**
多个 GEN 调用的输出契约是「正文 + 末尾一个 JSON 块」（权利要求书的
`{"invention_name", "claims"}`、章节生成的 `terms_delta` 等）——JSON 是给服务端
解析的，正文才是给人看的。而流式转发把每个分片原样推向 chat/doc 通道，
于是用户看到正文之后又滚出一大块 JSON（内容还和正文重复），
遇到不守围栏约定的模型时更是整段裸 JSON 混进段落里。

**边界。**
- 只影响**转发**：完整文本原样返回给调用方，解析不受任何影响；
- 静音是终结性的：按契约 JSON 尾巴只出现在末尾，一旦开始就不会再有正文；
- 围栏语言是判据：```json 静音，```mermaid 等其它围栏原样放行
  （交底书章节的框图/流程图就流在 doc 通道里，误杀它们比不修还糟）；
- 裸 JSON 只在**围栏之外**判（mermaid 体内以 `{` 开头的行不受牵连）。
"""

from __future__ import annotations

import json
import re

# 围栏行：```lang 或 ~~~lang（缩进 ≤3 空格，与 CommonMark 一致）
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*([\w-]*)")
# 裸 JSON 尾巴的起始行：以 { 开头且当行就有 "key": 的形状
_BARE_JSON_RE = re.compile(r'^\s*\{\s*"')


class TailMuter:
    """逐分片喂入，返回可转发给人看的部分；full text 由调用方自行累积。

    行缓冲：一行没结束前不转发（无法预知它是不是 JSON 的开头——
    转发出去的东西收不回来）。行都很短，对流式手感没有可感知影响。
    """

    def __init__(self) -> None:
        self._muted = False          # 终结性：尾巴开始后不再有正文
        self._pending = ""           # 当前未完行
        self._fence: str | None = None   # 当前所在围栏的语言（None = 不在围栏内）

    def _line_starts_tail(self, line: str) -> bool:
        fence = _FENCE_RE.match(line)
        if fence:
            lang = (fence.group(2) or "").lower()
            if self._fence is None:
                if lang == "json":
                    return True          # ```json 围栏 = 契约尾巴
                self._fence = lang or ""  # 进入其它围栏（mermaid / 空语言）
            else:
                self._fence = None        # 围栏关闭
            return False
        if self._fence is None and _BARE_JSON_RE.match(line):
            return True                   # 围栏外的裸 JSON 行
        return False

    def feed(self, delta: str) -> str:
        """喂入一个分片，返回其中可转发的部分（可能为空串）。"""
        if self._muted:
            return ""
        self._pending += delta
        out: list[str] = []
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            if self._line_starts_tail(line):
                self._muted = True
                self._pending = ""
                break
            out.append(line + "\n")
        return "".join(out)

    def flush(self) -> str:
        """流结束：把最后一个未完行放出来（除非它本身就是尾巴的开头）。"""
        if self._muted or not self._pending:
            return ""
        line, self._pending = self._pending, ""
        if self._line_starts_tail(line):
            return ""
        return line


# ---------------------------------------------------------------------------
# 交付边界：完整正文里的机器契约块清除
# ---------------------------------------------------------------------------

#: 已知的机器契约形状（键集）。**只删认得出的契约**——用户正文里合法的 JSON
#: 示例（交底书里举例的配置片段、接口报文）必须原样保留。
#: 误删正文比留下一个契约块更糟：前者丢的是内容，后者只是难看。
_CONTRACT_KEYS: tuple[frozenset[str], ...] = (
    frozenset({"terms_delta"}),
    frozenset({"add", "update"}),
    frozenset({"invention_name", "claims"}),
)

_ANY_FENCE_RE = re.compile(
    r"^ {0,3}```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\r?\n(.*?)\r?\n^ {0,3}```[ \t]*$",
    re.DOTALL | re.MULTILINE,
)


def _is_contract_payload(body: str) -> bool:
    """这块 JSON 是不是我们自己的机器契约。"""
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return False
    if not isinstance(data, dict):
        return False
    keys = set(data)
    return any(shape <= keys for shape in _CONTRACT_KEYS)


def strip_contract_blocks(text: str) -> str:
    """从**完整正文**里删掉机器契约 JSON 块（交付边界的兜底）。

    逐块剥离（terminology.extract_terms_delta）只认「文末最后一块」——那是对的，
    它要防的是误吞正文中间的示例。但模型不总把契约放在最末：尾巴后又补一句话、
    或者分两块吐，漏网的那块会一路进到 Word 和 PDF 里。

    交付物是要交到审查员手里的东西：**正文里出现 `"forbidden_variants": []`
    这类内部字段，比任何排版瑕疵都严重**。所以在装配这一层再兜一道，
    且只删「认得出是契约」的块，其余 JSON 与 mermaid 等围栏一律保留。
    """
    if not text or "```" not in text:
        return text

    def _sub(m: "re.Match[str]") -> str:
        lang = (m.group(1) or "").lower()
        if lang not in ("", "json"):
            return m.group(0)                 # mermaid 等其它围栏原样保留
        return "" if _is_contract_payload(m.group(2)) else m.group(0)

    out = _ANY_FENCE_RE.sub(_sub, text)
    if not out.strip():
        return out
    return re.sub(r"\n{3,}", "\n\n", out).strip() + "\n"
