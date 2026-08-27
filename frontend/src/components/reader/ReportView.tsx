import { useEffect, useRef, useState } from 'react'
import {
  ArrowsPointingOutIcon,
  ChevronDownIcon,
  ExclamationTriangleIcon,
  IdentificationIcon,
  LightBulbIcon,
  PhotoIcon,
  ScaleIcon,
} from '@heroicons/react/24/outline'
import type { ComponentType, SVGProps } from 'react'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { MarkdownBlock } from '../markdown/MarkdownBlock'
import { MermaidBlock } from '../markdown/MermaidBlock'
import { Modal } from '../ui/Modal'
import { ClaimTree } from './ClaimTree'
import { SECTION_ATTR } from './ReportToc'
import { mergeClaimTreeDetail } from '../../api/reader'
import type {
  ClaimTreeData,
  ReaderBlock,
  ReaderCalloutBlock,
  ReaderCalloutKind,
  ReaderReportMeta,
  ReaderSection,
  ReaderTableBlock,
} from '../../api/reader'

// ---------------------------------------------------------------------------
// callout 样式（report_template.md 映射：meta=indigo / claim=blue /
// grounding=emerald / warning=amber；figure 走中性灰）
// ---------------------------------------------------------------------------

interface CalloutStyle {
  wrap: string
  title: string
  Icon: ComponentType<SVGProps<SVGSVGElement>>
}

const CALLOUT_STYLES: Record<ReaderCalloutKind, CalloutStyle> = {
  meta: {
    wrap: 'border-l-indigo-400 dark:border-l-indigo-500/70 bg-indigo-50/60 dark:bg-indigo-500/10',
    title: 'text-[#6366f1] dark:text-indigo-400',
    Icon: IdentificationIcon,
  },
  claim: {
    wrap: 'border-l-blue-400 dark:border-l-blue-500/70 bg-blue-50/60 dark:bg-blue-500/10',
    title: 'text-blue-600 dark:text-blue-400',
    Icon: ScaleIcon,
  },
  grounding: {
    wrap: 'border-l-emerald-400 dark:border-l-emerald-500/70 bg-emerald-50/60 dark:bg-emerald-500/10',
    title: 'text-emerald-600 dark:text-emerald-400',
    Icon: LightBulbIcon,
  },
  warning: {
    wrap: 'border-l-amber-400 dark:border-l-amber-500/70 bg-amber-50/60 dark:bg-amber-500/10',
    title: 'text-amber-600 dark:text-amber-400',
    Icon: ExclamationTriangleIcon,
  },
  figure: {
    wrap: 'border-l-gray-300 dark:border-l-gray-600 bg-gray-50/70 dark:bg-gray-800/60',
    title: 'text-gray-600 dark:text-gray-300',
    Icon: PhotoIcon,
  },
}

function Callout({ block }: { block: ReaderCalloutBlock }) {
  const style = CALLOUT_STYLES[block.kind]
  const [open, setOpen] = useState(!block.collapsed)
  const title = block.title || zh.reader.report.calloutTitles[block.kind] || ''
  const { Icon } = style

  return (
    <div
      className={cn(
        'my-4 rounded-xl border border-l-4 border-gray-200/70 dark:border-gray-700/70 px-4 py-3',
        style.wrap,
      )}
    >
      <button
        type="button"
        onClick={() => block.collapsed && setOpen((v) => !v)}
        aria-expanded={open}
        className={cn(
          'w-full flex items-center gap-2 text-left text-[13px] font-semibold',
          style.title,
          !block.collapsed && 'cursor-default',
        )}
      >
        <Icon className="w-4 h-4 shrink-0" strokeWidth={1.5} />
        <span className="flex-1 min-w-0 truncate">{title}</span>
        {block.collapsed && (
          <ChevronDownIcon
            className={cn('w-4 h-4 shrink-0 transition-transform duration-200', open && 'rotate-180')}
            strokeWidth={1.5}
            aria-label={zh.reader.report.warningExpand}
          />
        )}
      </button>
      {open && block.markdown.trim() !== '' && (
        <MarkdownBlock className="md-prose mt-1.5 text-[13px]" markdown={block.markdown} />
      )}
    </div>
  )
}

function ReportTable({ block }: { block: ReaderTableBlock }) {
  return (
    <figure className="my-4">
      <div className="overflow-x-auto scrollbar-thin rounded-xl border border-gray-200 dark:border-gray-700">
        <table className="w-full border-collapse text-[13px]">
          {block.columns.length > 0 && (
            <thead>
              <tr>
                {block.columns.map((col, i) => (
                  <th
                    key={i}
                    className="bg-gray-50 dark:bg-gray-800 text-left font-semibold text-gray-900 dark:text-gray-100 px-3.5 py-2 border-b border-gray-200 dark:border-gray-700"
                  >
                    {col}
                  </th>
                ))}
              </tr>
            </thead>
          )}
          <tbody>
            {block.rows.map((row, i) => (
              <tr key={i}>
                {row.map((cell, j) => (
                  <td
                    key={j}
                    className="px-3.5 py-2 align-top text-gray-700 dark:text-gray-300 border-b border-gray-100 dark:border-gray-800 last:border-r-0"
                  >
                    {cell}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {block.caption && (
        <figcaption className="mt-1.5 text-xs text-gray-400 dark:text-gray-500">
          {block.caption}
        </figcaption>
      )}
    </figure>
  )
}

/**
 * 放大态图表：MermaidBlock 渲染后按 viewBox 还原 1:1 自然尺寸，
 * 超宽时由外层容器横向滚动（报告栏窄，缩略图看不清流程标签时用）。
 */
function NaturalMermaid({ code }: { code: string }) {
  const hostRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const host = hostRef.current
    if (!host) return
    // MermaidBlock 异步渲染，SVG 落地时机不定 → 观察子树变化后再量
    const apply = (): void => {
      const svg = host.querySelector('svg')
      const width = svg?.viewBox?.baseVal?.width ?? 0
      if (!svg || width <= 0) return
      svg.style.minWidth = `${Math.round(width)}px`
      svg.style.maxWidth = 'none'
    }
    apply()
    const observer = new MutationObserver(apply)
    observer.observe(host, { childList: true, subtree: true })
    return () => observer.disconnect()
  }, [code])

  return (
    <div ref={hostRef} className="overflow-x-auto scrollbar-thin">
      <MermaidBlock code={code} className="my-0" />
    </div>
  )
}

/** 报告内的 mermaid：栏内缩略呈现 + 右上角放大钮（Modal 内 1:1 查看）。 */
function ReportMermaid({ code }: { code: string }) {
  const [zoomed, setZoomed] = useState(false)

  return (
    <div className="relative group/mermaid">
      <MermaidBlock code={code} />
      <button
        type="button"
        onClick={() => setZoomed(true)}
        aria-label={zh.reader.report.mermaidZoom}
        title={zh.reader.report.mermaidZoom}
        className={cn(
          'absolute top-2 right-2 w-7 h-7 rounded-lg flex items-center justify-center',
          'bg-white/85 dark:bg-gray-800/85 backdrop-blur-sm',
          'border border-gray-200 dark:border-gray-700 text-gray-500 dark:text-gray-400',
          'opacity-0 group-hover/mermaid:opacity-100 focus-visible:opacity-100 touch:opacity-100',
          'hover:text-gray-700 dark:hover:text-gray-200 transition-opacity',
        )}
      >
        <ArrowsPointingOutIcon className="w-4 h-4" strokeWidth={1.5} />
      </button>
      <Modal
        open={zoomed}
        onClose={() => setZoomed(false)}
        title={zh.reader.report.mermaidZoomTitle}
        panelClassName="max-w-5xl"
      >
        <div className="max-h-[75vh] overflow-y-auto scrollbar-thin">
          <NaturalMermaid code={code} />
        </div>
      </Modal>
    </div>
  )
}

function BlockView({
  block,
  claimTree,
}: {
  block: ReaderBlock
  /** 已确认的完整权要树：为报告内的树形表补齐前序与技术特征。 */
  claimTree?: ClaimTreeData | null
}) {
  switch (block.type) {
    case 'markdown':
      return <MarkdownBlock className="md-prose" markdown={block.markdown} />
    case 'table':
      return <ReportTable block={block} />
    case 'mermaid':
      return <ReportMermaid code={block.code} />
    case 'claim_tree':
      return (
        <ClaimTree tree={mergeClaimTreeDetail(block.tree, claimTree)} showSummary className="my-4" />
      )
    case 'callout':
      return <Callout block={block} />
  }
}

/** report.json meta → 著录项卡片（报告自身未带 callout-meta 时补渲）。 */
function MetaCard({ meta }: { meta: ReaderReportMeta }) {
  const rows: [string, string][] = []
  const push = (key: keyof ReaderReportMeta, value: string | undefined) => {
    if (value) rows.push([zh.reader.report.metaFields[key] ?? key, value])
  }
  push('pub_number', meta.pub_number)
  push('patent_type', meta.patent_type)
  push('domain', meta.domain)
  push('ipc', meta.ipc)
  push('assignees', meta.assignees?.join('、'))
  push('pub_date', meta.pub_date)
  push('perspective', meta.perspective)
  push(
    'evidence_scope',
    meta.evidence_scope
      ? (zh.reader.report.evidenceScopes[meta.evidence_scope] ?? meta.evidence_scope)
      : undefined,
  )
  push('read_date', meta.read_date)
  if (rows.length === 0) return null

  const style = CALLOUT_STYLES.meta
  return (
    <div
      className={cn(
        'my-4 rounded-xl border border-l-4 border-gray-200/70 dark:border-gray-700/70 px-4 py-3',
        style.wrap,
      )}
    >
      <div className={cn('flex items-center gap-2 text-[13px] font-semibold', style.title)}>
        <style.Icon className="w-4 h-4 shrink-0" strokeWidth={1.5} />
        {zh.reader.report.calloutTitles.meta}
        {meta.confidence_speculative && (
          <span className="ml-auto text-[10px] font-medium px-1.5 py-0.5 rounded bg-amber-50 text-amber-600 dark:bg-amber-500/10 dark:text-amber-400">
            {zh.reader.report.speculative}
          </span>
        )}
      </div>
      <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[13px]">
        {rows.map(([label, value]) => (
          <div key={label} className="contents">
            <dt className="text-gray-400 dark:text-gray-500 whitespace-nowrap">{label}</dt>
            <dd className="text-gray-700 dark:text-gray-300 min-w-0 break-words">{value}</dd>
          </div>
        ))}
      </dl>
    </div>
  )
}

export interface ReportViewProps {
  sections: ReaderSection[]
  meta?: ReaderReportMeta | null
  /** 已确认的完整权要树（为报告内的树形表补齐前序与技术特征）。 */
  claimTree?: ClaimTreeData | null
  /** C7 自检未决事项（不入正文，列在报告末尾）。 */
  unresolved?: string[]
  /** 生成中：尾部闪烁光标。 */
  streaming?: boolean
  className?: string
}

/**
 * §3.4 解读报告正文：按 report.json 的 blocks 渲染——
 * markdown（复用 MarkdownBlock）/ table / claim_tree（ClaimTree 组件）/
 * mermaid（复用 MermaidBlock）/ 四种 callout（带左边框与图标的提示块）。
 * 每节写入 `data-report-section` 锚点供 ReportToc 做 scroll-spy。
 */
export function ReportView({
  sections,
  meta,
  claimTree,
  unresolved,
  streaming = false,
  className,
}: ReportViewProps) {
  const hasMetaCallout = sections.some((s) =>
    s.blocks.some((b) => b.type === 'callout' && b.kind === 'meta'),
  )

  return (
    <div className={cn('min-w-0', className)}>
      {meta?.title && (
        <h1 className="text-lg font-bold tracking-tight text-gray-900 dark:text-gray-100">
          {meta.title}
        </h1>
      )}
      {meta && !hasMetaCallout && <MetaCard meta={meta} />}

      {sections.map((section, index) => (
        <section key={section.id} {...{ [SECTION_ATTR]: section.id }} className="scroll-mt-2">
          {section.title && (
            <h2
              className={cn(
                'text-[15px] font-semibold tracking-tight text-gray-900 dark:text-gray-100',
                'pb-1.5 mb-3 border-b border-gray-200 dark:border-gray-700',
                index === 0 ? 'mt-0' : 'mt-7',
              )}
            >
              {section.title}
            </h2>
          )}
          {section.blocks.map((block, i) => (
            <BlockView key={i} block={block} claimTree={claimTree} />
          ))}
        </section>
      ))}

      {unresolved && unresolved.length > 0 && (
        <div
          className={cn(
            'mt-7 rounded-xl border border-l-4 border-gray-200/70 dark:border-gray-700/70 px-4 py-3',
            CALLOUT_STYLES.warning.wrap,
          )}
        >
          <div
            className={cn(
              'flex items-center gap-2 text-[13px] font-semibold',
              CALLOUT_STYLES.warning.title,
            )}
          >
            <ExclamationTriangleIcon className="w-4 h-4 shrink-0" strokeWidth={1.5} />
            {zh.reader.report.unresolvedTitle(unresolved.length)}
          </div>
          <ul className="mt-1.5 space-y-1 text-[13px] text-gray-700 dark:text-gray-300 list-disc pl-5">
            {unresolved.map((item, i) => (
              <li key={i}>{item}</li>
            ))}
          </ul>
        </div>
      )}

      {streaming && <span className="md-cursor" aria-hidden="true" />}
    </div>
  )
}
