/**
 * 把 LaTeX 原生分隔符归一化为 remark-math 认识的 `$` 系分隔符。
 *
 * 专利文书的写作规范要求「行内公式以 \(...\) 标记，独立公式以 \[...\] 标记」，
 * 模型也照此输出。但这在 Markdown 渲染链上有两重障碍：
 *
 *  1. remark-math 只认 `$...$` / `$$...$$`；
 *  2. 更隐蔽的是，CommonMark 把 `\(` `\[` 视为**转义字符**，parse 阶段就把反斜杠
 *     吃掉了——页面上会看到 `(p)`、`(\alpha)` 这种残缺文本。因此不能靠 mdast
 *     transformer 事后补救，必须在交给解析器之前处理原始文本。
 *
 * 与后端 md_to_docx.py 的 `_normalize_math_delimiters` 是同一套规则，
 * 保证「预览所见」与「导出 Word 所得」一致。Markdown 原文（交付物）不受影响，
 * 归一化只作用于渲染用的内存副本。
 */

const FENCE_RE = /^\s*(?:```|~~~)/
/** 整行就是一条独立公式：\[ ... \] */
const ONELINE_DISPLAY_RE = /^\s*\\\[(.+?)\\\]\s*$/
/** 独占一行的开 / 闭分隔符（多行公式写法） */
const OPEN_ONLY_RE = /^\s*\\\[\s*$/
const CLOSE_ONLY_RE = /^\s*\\\]\s*$/
/** 行内 \( ... \)（只在同一行内配对：跨行放开会把整段文字误吞成公式） */
const INLINE_PAREN_RE = /\\\((.+?)\\\)/g

export function normalizeMathDelimiters(md: string): string {
  if (!md.includes('\\[') && !md.includes('\\(')) return md

  const out: string[] = []
  let inFence = false
  for (const line of md.split('\n')) {
    if (FENCE_RE.test(line)) {
      inFence = !inFence
      out.push(line)
      continue
    }
    if (inFence) {
      out.push(line)
      continue
    }
    const m = ONELINE_DISPLAY_RE.exec(line)
    if (m) {
      // 拆成独占三行，remark-math 才会按块级公式（display）解析，
      // \tag{n} 也只有在 display 模式下才是合法的 KaTeX。
      out.push('$$', m[1].trim(), '$$')
      continue
    }
    if (OPEN_ONLY_RE.test(line) || CLOSE_ONLY_RE.test(line)) {
      out.push('$$')
      continue
    }
    // 行内公式：`$...$` 不能跨行，把内部换行压成空格
    out.push(line.replace(INLINE_PAREN_RE, (_, body: string) => `$${body.trim().split(/\s+/).join(' ')}$`))
  }
  return out.join('\n')
}
