/**
 * 流式 markdown → 报告 sections/blocks（§3.4）。
 *
 * 后端 C6 才把报告装配为 report.json；在 `note` 步骤流式产出期间，
 * 前端按同一 block 模型即时解析 doc 通道文本，使目录、权要树与四种 callout
 * 在生成过程中就能呈现（报告落地后再由 report.json 覆盖，渲染路径完全一致）。
 *
 * 解析规则与 report_template.md「callout → 站内 block 类型映射」对齐：
 * - ```mermaid 围栏           → mermaid block（未闭合时交给 MermaidBlock 显骨架）
 * - ```claim_tree 围栏（JSON）→ claim_tree block
 * - `> [!patent-meta] 标题`   → callout block（`-` 后缀 = 默认折叠）
 * - `## ` 起新节；首个 `## ` 之前的内容归入无标题前言节（不进目录）
 */

import { normalizeCalloutKind, normalizeClaimTree } from '../../api/reader'
import type { ClaimTreeData, ReaderBlock, ReaderSection } from '../../api/reader'

const FENCE_RE = /^ {0,3}(`{3,}|~{3,})\s*([\w-]*)\s*$/
const CALLOUT_RE = /^>\s*\[!([\w-]+)\]([-+]?)\s*(.*)$/
const SECTION_RE = /^##\s+(.*)$/
const CLAIM_TREE_LANG_RE = /^claim[-_]?tree$/i

/** 第三节（树形表由平台装配）的标题特征。 */
const CLAIM_TREE_SECTION_RE = /权利要求树/

interface Draft {
  id: string
  title: string
  blocks: ReaderBlock[]
}

/**
 * 解析流式报告 markdown。
 * @param markdown doc 通道累积文本（可能不完整）
 * @param fallbackTree 已确认的权要树；第三节缺 claim_tree block 时补入
 */
export function parseReportMarkdown(
  markdown: string,
  fallbackTree?: ClaimTreeData | null,
): ReaderSection[] {
  if (!markdown.trim()) return []

  const lines = markdown.split('\n')
  const sections: ReaderSection[] = []
  let sectionSeq = 0
  let current: Draft = { id: 'overview', title: '', blocks: [] }
  let buffer: string[] = []

  const flushMarkdown = (): void => {
    const text = buffer.join('\n').trim()
    buffer = []
    if (text) current.blocks.push({ type: 'markdown', markdown: text })
  }

  const closeSection = (): void => {
    flushMarkdown()
    if (current.blocks.length > 0 || current.title) sections.push(current)
  }

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i]

    // --- 围栏块 ---------------------------------------------------------
    const fence = FENCE_RE.exec(line)
    if (fence) {
      const marker = fence[1]
      const lang = fence[2].toLowerCase()
      const body: string[] = []
      let closed = false
      let j = i + 1
      for (; j < lines.length; j += 1) {
        const close = FENCE_RE.exec(lines[j])
        if (close && close[1][0] === marker[0] && close[1].length >= marker.length && !close[2]) {
          closed = true
          break
        }
        body.push(lines[j])
      }
      const code = body.join('\n')

      if (lang === 'mermaid') {
        flushMarkdown()
        // 未闭合时也交给 MermaidBlock（内部显示「图表生成中…」骨架）
        current.blocks.push({ type: 'mermaid', code })
      } else if (CLAIM_TREE_LANG_RE.test(lang)) {
        flushMarkdown()
        if (closed) {
          const tree = safeTree(code)
          if (tree) current.blocks.push({ type: 'claim_tree', tree })
        }
      } else {
        // 其余围栏原样留在 markdown 内（CodeBlock 渲染）
        buffer.push(line, ...body)
        if (closed) buffer.push(lines[j])
      }
      i = closed ? j : lines.length
      continue
    }

    // --- callout 块 -----------------------------------------------------
    const callout = CALLOUT_RE.exec(line)
    if (callout) {
      flushMarkdown()
      const inner: string[] = []
      let j = i + 1
      for (; j < lines.length; j += 1) {
        if (!lines[j].startsWith('>')) break
        inner.push(lines[j].replace(/^>\s?/, ''))
      }
      current.blocks.push({
        type: 'callout',
        kind: normalizeCalloutKind(callout[1].toLowerCase()),
        title: callout[3].trim(),
        markdown: inner.join('\n').trim(),
        collapsed: callout[2] === '-',
      })
      i = j - 1
      continue
    }

    // --- 节标题 ---------------------------------------------------------
    const section = SECTION_RE.exec(line)
    if (section) {
      closeSection()
      sectionSeq += 1
      current = { id: `sec-${sectionSeq}`, title: section[1].trim(), blocks: [] }
      continue
    }

    buffer.push(line)
  }
  closeSection()

  return fallbackTree ? withFallbackTree(sections, fallbackTree) : sections
}

function safeTree(json: string): ClaimTreeData | null {
  try {
    return normalizeClaimTree(JSON.parse(json))
  } catch {
    return null
  }
}

/** 第三节没有 claim_tree block 时补入已确认的权要树（后端由平台装配，流式期间先补）。 */
function withFallbackTree(sections: ReaderSection[], tree: ClaimTreeData): ReaderSection[] {
  if (tree.claims.length === 0) return sections
  const hasTree = sections.some((s) => s.blocks.some((b) => b.type === 'claim_tree'))
  if (hasTree) return sections
  const index = sections.findIndex((s) => CLAIM_TREE_SECTION_RE.test(s.title))
  if (index < 0) return sections
  const target = sections[index]
  const patched: ReaderSection = {
    ...target,
    blocks: [...target.blocks, { type: 'claim_tree', tree }],
  }
  return sections.map((s, i) => (i === index ? patched : s))
}
