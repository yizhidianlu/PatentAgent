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
