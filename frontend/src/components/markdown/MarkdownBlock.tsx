import { isValidElement, memo, type ReactElement, type ReactNode } from 'react'
import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import 'katex/dist/katex.min.css'
import './markdown.css'
import { cn } from '../../lib/cn'
import { CodeBlock } from './CodeBlock'
import { MermaidBlock } from './MermaidBlock'
import { normalizeMathDelimiters } from './normalizeMath'

const remarkPlugins = [remarkGfm, remarkMath]
const rehypePlugins = [rehypeKatex]

/** 递归拍平 ReactNode 为纯文本（码块内容提取）。 */
function extractText(node: ReactNode): string {
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(extractText).join('')
  if (isValidElement(node)) {
    return extractText((node.props as { children?: ReactNode }).children)
  }
  return ''
}

const LANGUAGE_RE = /language-([\w-]+)/

const components: Components = {
  // 围栏码块：<pre><code class="language-x"> → CodeBlock / MermaidBlock
  pre({ children }) {
    let className = ''
    let codeNode: ReactNode = children
    const childArray = Array.isArray(children) ? children : [children]
    const codeEl = childArray.find((c): c is ReactElement => isValidElement(c))
    if (codeEl) {
      const props = codeEl.props as { className?: string; children?: ReactNode }
      className = props.className ?? ''
      codeNode = props.children
    }
    const code = extractText(codeNode).replace(/\n$/, '')
    const language = LANGUAGE_RE.exec(className)?.[1]
    if (language === 'mermaid') return <MermaidBlock code={code} />
    return <CodeBlock code={code} language={language} />
  },
  // 行内 code：样式由 .md-prose code 接管
  code({ className, children }) {
    return <code className={className}>{children}</code>
  },
  // 表格外包滚动容器（§2.7 表格描边 + 横向滚动）
  table({ children }) {
    return (
      <div className="md-table-wrap scrollbar-thin">
        <table>{children}</table>
      </div>
    )
  },
}

export interface MarkdownBlockProps {
  markdown: string
  /** 独立使用（非 StreamingMarkdown 内）时补 md-prose 获得 typography。 */
  className?: string
}

/**
 * §2.7 MarkdownBlock：react-markdown + remark-gfm + remark-math + rehype-katex。
 * memo：内容不变则整块跳过重渲（StreamingMarkdown 的稳定块）。
 */
export const MarkdownBlock = memo(function MarkdownBlock({
  markdown,
  className,
}: MarkdownBlockProps) {
  return (
    <div className={cn('md-block', className)}>
      <ReactMarkdown
        remarkPlugins={remarkPlugins}
        rehypePlugins={rehypePlugins}
        components={components}
      >
        {normalizeMathDelimiters(markdown)}
      </ReactMarkdown>
    </div>
  )
})
