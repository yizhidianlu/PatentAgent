import { isValidElement, memo, useState, type ReactElement, type ReactNode } from 'react'
import ReactMarkdown, { defaultUrlTransform, type Components, type UrlTransform } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import 'katex/dist/katex.min.css'
import './markdown.css'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { CodeBlock } from './CodeBlock'
import { MermaidBlock } from './MermaidBlock'
import { normalizeMathDelimiters } from './normalizeMath'
import { useMediaCaseId } from '../../lib/mediaContext'
import { toMediaUrl } from '../../lib/mediaUrl'

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

/**
 * 正文插图。
 *
 * md 里写的是磁盘路径（导出 Word/PDF 时按盘读），浏览器取不到，
 * 这里改写成案件媒体端点。拿不到案件上下文时不出 `<img>` ——
 * 一个必然 404 的破图比只留图题更难看，也更让人以为是数据丢了。
 */
function MarkdownImage({ src, alt, title }: { src?: string; alt?: string; title?: string }) {
  const caseId = useMediaCaseId()
  const [failed, setFailed] = useState(false)
  const raw = typeof src === 'string' ? src : ''
  const url = toMediaUrl(caseId, raw)

  /*
   * 取不到图时**明说**，不要静静地什么都不显示。
   *
   * 早先这里是「拿不到 caseId 就 return null、加载失败就 display:none」——
   * 于是页面上只剩一行图题，看起来像「这张图本来就没有」。
   * 用户报的正是这个：右侧没有显示出图片，而界面一个字都没解释为什么。
   * 静默失败在这个项目里已经咬过好几次，这里不再重复。
   */
  if (!url || failed) {
    return (
      <span className="md-figure-missing" role="img" aria-label={alt || zh.document.figureFailed}>
        <span className="md-figure-missing-title">{alt || zh.document.figureFailed}</span>
        <span className="md-figure-missing-hint">
          {!caseId ? zh.document.figureNoCase : zh.document.figureFailedHint}
        </span>
        {raw && <code className="md-figure-missing-path">{raw}</code>}
      </span>
    )
  }

  return (
    <img
      className="md-figure"
      src={url}
      alt={alt ?? ''}
      title={title}
      loading="lazy"
      onError={() => setFailed(true)}
    />
  )
}

/**
 * 图片的 src 放行给 MarkdownImage 自己收口（默认清洗器会把 `C:/…` 当未知协议丢掉）；
 * 其余（尤其是链接 href）仍走 react-markdown 的默认清洗，`javascript:` 照样拦。
 */
const urlTransform: UrlTransform = (value, key, node) => {
  if (key === 'src' && node.tagName === 'img') return value
  return defaultUrlTransform(value)
}

const components: Components = {
  img: MarkdownImage,
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
        urlTransform={urlTransform}
      >
        {normalizeMathDelimiters(markdown)}
      </ReactMarkdown>
    </div>
  )
})
