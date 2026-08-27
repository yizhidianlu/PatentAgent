import { useMemo, useRef, useState } from 'react'
import {
  ArrowDownTrayIcon,
  ChevronDoubleLeftIcon,
  ChevronDoubleRightIcon,
  DocumentTextIcon,
} from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { Badge } from '../ui/Badge'
import { EmptyState } from '../ui/EmptyState'
import { Skeleton } from '../ui/Skeleton'
import { ReportToc } from './ReportToc'
import { ReportView } from './ReportView'
import { parseReportMarkdown } from './parseReport'
import type { ClaimTreeData, ReaderReport } from '../../api/reader'

export interface ReportPanelProps {
  title?: string
  /** doc 通道累积的报告 markdown（生成中即时解析）。 */
  markdown: string
  streaming?: boolean
  version?: number
  /** 已装配的 report.json（就绪后优先于流式解析结果）。 */
  report?: ReaderReport | null
  /** 已确认的权要树：第三节缺 claim_tree block 时补入，并为树形表补齐可展开的全文。 */
  claimTree?: ClaimTreeData | null
  /** C7 自检未决事项（不入正文，列在报告末尾）。 */
  unresolved?: string[]
  onExportMd?: () => void
  className?: string
}

/**
 * §3.4 解读工作台主面：报告面板（xl 以上常驻右侧）。
 * 头 `h-12 px-4`（标题 + V 徽章 + 生成中徽章 + 导出 MD + 收起）；
 * 体 = 左侧常驻 ReportToc（scroll-spy）+ 右侧 ReportView 滚动区。
 */
export function ReportPanel({
  title = zh.reader.panel.title,
  markdown,
  streaming = false,
  version,
  report,
  claimTree,
  unresolved,
  onExportMd,
  className,
}: ReportPanelProps) {
  const [collapsed, setCollapsed] = useState(false)
  const scrollerRef = useRef<HTMLDivElement>(null)

  const sections = useMemo(
    () => report?.sections ?? parseReportMarkdown(markdown, claimTree),
    [report, markdown, claimTree],
  )

  if (collapsed) {
    return (
      <div
        className={cn(
          'hidden xl:flex flex-col items-center border-l border-gray-200 dark:border-gray-800',
          'bg-white dark:bg-gray-900 w-10 py-3 gap-3',
          className,
        )}
      >
        <button
          type="button"
          onClick={() => setCollapsed(false)}
          aria-label={zh.reader.panel.expand}
          title={zh.reader.panel.expand}
          className="w-8 h-8 rounded-full flex items-center justify-center text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors"
        >
          <ChevronDoubleLeftIcon className="w-4 h-4" strokeWidth={1.5} />
        </button>
        <DocumentTextIcon className="w-5 h-5 text-gray-400" strokeWidth={1.5} />
      </div>
    )
  }

  const iconBtn =
    'w-8 h-8 rounded-full flex items-center justify-center text-gray-500 dark:text-gray-400 ' +
    'hover:bg-gray-100 dark:hover:bg-gray-800 hover:text-gray-700 dark:hover:text-gray-200 transition-colors'

  return (
    <aside
      className={cn(
        'hidden xl:flex w-[50%] max-w-[820px] flex-col min-h-0',
        'border-l border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900',
        className,
      )}
    >
      {/* 头部 */}
      <div className="h-12 shrink-0 px-4 flex items-center gap-2 border-b border-gray-100 dark:border-gray-800">
        <DocumentTextIcon className="w-4 h-4 shrink-0 text-gray-400" strokeWidth={1.5} />
        <h2 className="text-sm font-semibold text-gray-900 dark:text-gray-100 truncate">{title}</h2>
        {version !== undefined && (
          <Badge variant="gradient" className="shrink-0">
            {zh.document.versionBadge(version)}
          </Badge>
        )}
        {streaming && (
          <span className="flex items-center gap-1 text-[10px] font-medium text-[#6366f1] dark:text-indigo-400 shrink-0">
            <span className="w-1.5 h-1.5 rounded-full bg-[#6366f1] animate-pulse" />
            {zh.document.streamingBadge}
          </span>
        )}
        <div className="flex-1" />
        <button
          type="button"
          onClick={onExportMd}
          aria-label={zh.reader.panel.exportMd}
          title={zh.reader.panel.exportMd}
          className={iconBtn}
        >
          <ArrowDownTrayIcon className="w-4 h-4" strokeWidth={1.5} />
        </button>
        <button
          type="button"
          onClick={() => setCollapsed(true)}
          aria-label={zh.reader.panel.collapse}
          title={zh.reader.panel.collapse}
          className={iconBtn}
        >
          <ChevronDoubleRightIcon className="w-4 h-4" strokeWidth={1.5} />
        </button>
      </div>

      {/* 生成中活动条 */}
      {streaming && (
        <div
          className="h-0.5 shrink-0 bg-gradient-to-r from-blue-500 via-purple-500 to-blue-500 animate-badge-shimmer bg-[length:200%_100%]"
          aria-hidden="true"
        />
      )}

      {/* 体：目录 + 正文 */}
      <div className="flex-1 min-h-0 flex overflow-hidden">
        {sections.length > 0 && (
          <div className="hidden lg:block shrink-0 overflow-y-auto scrollbar-thin border-r border-gray-100 dark:border-gray-800 py-6 px-3">
            <ReportToc sections={sections} scrollerRef={scrollerRef} />
          </div>
        )}
        <div
          ref={scrollerRef}
          className="flex-1 min-w-0 min-h-0 overflow-y-auto scrollbar-thin px-6 py-6"
        >
          {sections.length > 0 ? (
            <ReportView
              sections={sections}
              meta={report?.meta}
              claimTree={claimTree}
              unresolved={unresolved}
              streaming={streaming}
            />
          ) : streaming ? (
            // 生成已开始但首个小节尚未成形：骨架屏，别回落到「尚未生成」空态
            <div className="space-y-4" aria-busy="true" aria-label={zh.reader.panel.loading}>
              <Skeleton shimmer className="h-6 w-2/5 rounded" />
              <Skeleton shimmer className="h-4 w-full rounded" />
              <Skeleton shimmer className="h-4 w-[92%] rounded" />
              <Skeleton shimmer className="h-4 w-[78%] rounded" />
              <Skeleton shimmer className="h-24 w-full rounded-xl" />
              <Skeleton shimmer className="h-4 w-[88%] rounded" />
              <Skeleton shimmer className="h-4 w-[64%] rounded" />
            </div>
          ) : (
            <EmptyState
              icon={DocumentTextIcon}
              title={zh.reader.panel.emptyTitle}
              description={zh.reader.panel.emptyDesc}
              className="h-full"
            />
          )}
        </div>
      </div>
    </aside>
  )
}
