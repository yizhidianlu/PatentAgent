import { useMemo, type ReactNode } from 'react'
import { cn } from '../../lib/cn'
import './markdown.css'
import { MarkdownBlock } from './MarkdownBlock'
import { MermaidBlock } from './MermaidBlock'

// ---------------------------------------------------------------------------
// 块切分（§2.7：按 \n\n 切稳定块，码块围栏内不切）
// ---------------------------------------------------------------------------

interface FenceState {
  char: string
  len: number
}

const FENCE_RE = /^ {0,3}(`{3,}|~{3,})(.*)$/

/**
 * 按空行（码块围栏外）切块。围栏内的空行不产生切分，
 * 稳定块内容在流式追加时保持不变 → memo 跳过重渲。
 */
export function splitMarkdownBlocks(markdown: string): string[] {
  if (!markdown) return []
  const lines = markdown.split('\n')
  const blocks: string[] = []
  let current: string[] = []
  let fence: FenceState | null = null

  const flushBlock = (): void => {
    if (current.length > 0) {
      blocks.push(current.join('\n'))
      current = []
    }
  }

  for (const line of lines) {
    const m = FENCE_RE.exec(line)
    if (fence) {
      current.push(line)
      // 关闭围栏：同字符、长度不小于开栏、且行内仅围栏字符
      if (m && m[1][0] === fence.char && m[1].length >= fence.len && m[2].trim() === '') {
        fence = null
      }
      continue
    }
    if (m) {
      fence = { char: m[1][0], len: m[1].length }
      current.push(line)
      continue
    }
    if (line.trim() === '') {
      flushBlock()
      continue
    }
    current.push(line)
  }
  flushBlock()
  return blocks
}

interface UnclosedFence {
  /** 开栏行在文本中的字符偏移。 */
  index: number
  lang: string
  /** 开栏行之后的（不完整）围栏体。 */
  body: string
}

/** 文本末尾是否有未闭合围栏（流式尾块专用）。 */
function findUnclosedFence(markdown: string): UnclosedFence | null {
  const lines = markdown.split('\n')
  let fence: (FenceState & { index: number; lang: string; bodyStart: number }) | null = null
  let offset = 0

  for (const line of lines) {
    const m = FENCE_RE.exec(line)
    if (fence) {
      if (m && m[1][0] === fence.char && m[1].length >= fence.len && m[2].trim() === '') {
        fence = null
      }
    } else if (m) {
      fence = {
        char: m[1][0],
        len: m[1].length,
        index: offset,
        lang: m[2].trim().split(/\s+/)[0] ?? '',
        bodyStart: offset + line.length + 1,
      }
    }
    offset += line.length + 1
  }

  if (!fence) return null
  return {
    index: fence.index,
    lang: fence.lang,
    body: markdown.slice(Math.min(fence.bodyStart, markdown.length)),
  }
}

// ---------------------------------------------------------------------------
// 组件
// ---------------------------------------------------------------------------

export interface StreamingMarkdownProps {
  markdown: string
  /** 流式中：尾块随 delta 重渲 + 闪烁光标；未闭合 mermaid 显示骨架。 */
  streaming?: boolean
  className?: string
}

/**
 * §2.7 StreamingMarkdown：
 * - 按 `\n\n`（码块围栏外）切稳定块，除尾块外 memo 渲染；
 * - 流式中尾部闪烁光标；
 * - 尾块中未闭合的 ```mermaid 围栏以骨架占位（闭合后才真正渲染）。
 */
export function StreamingMarkdown({
  markdown,
  streaming = false,
  className,
}: StreamingMarkdownProps) {
  const blocks = useMemo(() => splitMarkdownBlocks(markdown), [markdown])

  let stable = blocks
  let tailNode: ReactNode = null

  if (streaming && blocks.length > 0) {
    stable = blocks.slice(0, -1)
    const tail = blocks[blocks.length - 1]
    const unclosed = findUnclosedFence(tail)
    if (unclosed && unclosed.lang === 'mermaid') {
      // 未闭合 mermaid：围栏前正文照常渲染，围栏体以骨架占位
      const before = tail.slice(0, unclosed.index).trimEnd()
      tailNode = (
        <>
          {before !== '' && <MarkdownBlock markdown={before} />}
          <MermaidBlock code={unclosed.body} pending />
        </>
      )
    } else {
      // 尾块（含未闭合普通码块——流式代码直接可见）随 delta 重渲
      tailNode = <MarkdownBlock markdown={tail} />
    }
  }

  return (
    <div className={cn('md-prose', className)}>
      {stable.map((block, i) => (
        <MarkdownBlock key={i} markdown={block} />
      ))}
      {tailNode}
      {streaming && <span className="md-cursor" aria-hidden="true" />}
    </div>
  )
}
