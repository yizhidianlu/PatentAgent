import { useState } from 'react'
import {
  ArrowDownTrayIcon,
  ArrowsPointingOutIcon,
  ChevronDoubleLeftIcon,
  ChevronDoubleRightIcon,
  ClockIcon,
  DocumentTextIcon,
} from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { Badge } from '../ui/Badge'
import { Dropdown } from '../ui/Dropdown'
import { EmptyState } from '../ui/EmptyState'
import { Modal } from '../ui/Modal'
import { Skeleton } from '../ui/Skeleton'
import { StreamingMarkdown } from '../markdown/StreamingMarkdown'
import { DownloadMenu } from './DownloadMenu'
import { VersionHistory, type VersionEntry } from './VersionHistory'
import type { DownloadFormat } from '../../lib/download'

/** DocumentPanel 呈现所需的文档快照（对应 sessionStore.DocState）。 */
export interface PanelDoc {
  markdown: string
  streaming: boolean
  version?: number
  filename?: string
}

export interface DocumentPanelProps {
  /** 头部标题（缺省「文档」）。 */
  title?: string
  doc: PanelDoc | null
  /** 版本历史（由新到旧；§2.10 行样式，数据经 props）。 */
  versions?: VersionEntry[]
  onDownload?: (format: DownloadFormat) => void
  onVersionDownload?: (entry: VersionEntry, format: DownloadFormat) => void
  /** 收起态（parent 控制布局宽度）。 */
  collapsed?: boolean
  onToggleCollapse?: () => void
  className?: string
}

/**
 * §3.2 右侧文档面板：
 * 头 `h-12 px-4`（标题 + V 徽章 + 流式徽章 + 版本历史下拉 + 下载 + 全屏 Modal + 收起）；
 * 流式中头下 `h-0.5` 渐变活动条（animate-badge-shimmer）；体 `px-8 py-6` StreamingMarkdown。
 */
export function DocumentPanel({
  title = zh.document.panelTitle,
  doc,
  versions = [],
  onDownload,
  onVersionDownload,
  collapsed = false,
  onToggleCollapse,
  className,
}: DocumentPanelProps) {
  const [fullscreen, setFullscreen] = useState(false)

  if (collapsed) {
    return (
      <div
        className={cn(
          'flex flex-col items-center border-l border-gray-200 dark:border-gray-800',
          'bg-white dark:bg-gray-900 w-10 py-3 gap-3',
          className,
        )}
      >
        <button
          type="button"
          onClick={onToggleCollapse}
          aria-label={zh.document.expand}
          title={zh.document.expand}
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

  // 文档流已开启但首个 delta 尚未到达：骨架屏（比空白正文更能说明「正在写」）
  const awaitingFirstDelta = Boolean(doc?.streaming) && !doc?.markdown.trim()

  const body = !doc ? (
    <EmptyState
      icon={DocumentTextIcon}
      title={zh.document.emptyTitle}
      description={zh.document.emptyDesc}
      className="h-full"
    />
  ) : awaitingFirstDelta ? (
    <div className="space-y-4" aria-busy="true" aria-label={zh.document.loading}>
      <Skeleton shimmer className="h-6 w-2/5 rounded" />
      <Skeleton shimmer className="h-4 w-full rounded" />
      <Skeleton shimmer className="h-4 w-[92%] rounded" />
      <Skeleton shimmer className="h-4 w-[74%] rounded" />
      <Skeleton shimmer className="h-28 w-full rounded-xl" />
      <Skeleton shimmer className="h-4 w-[86%] rounded" />
    </div>
  ) : (
    <StreamingMarkdown markdown={doc.markdown} streaming={doc.streaming} />
  )

  return (
    <aside
      className={cn(
        'flex flex-col min-h-0 border-l border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900',
        className,
      )}
    >
      {/* 头部 */}
      <div className="h-12 shrink-0 px-4 flex items-center gap-2 border-b border-gray-100 dark:border-gray-800">
        <DocumentTextIcon className="w-4 h-4 shrink-0 text-gray-400" strokeWidth={1.5} />
        <h2 className="text-sm font-semibold text-gray-900 dark:text-gray-100 truncate">
          {title}
        </h2>
        {doc?.version !== undefined && (
          <Badge variant="gradient" className="shrink-0">
            {zh.document.versionBadge(doc.version)}
          </Badge>
        )}
        {doc?.streaming && (
          <span className="flex items-center gap-1 text-[10px] font-medium text-[#6366f1] dark:text-indigo-400 shrink-0">
            <span className="w-1.5 h-1.5 rounded-full bg-[#6366f1] animate-pulse" />
            {zh.document.streamingBadge}
          </span>
        )}
        <div className="flex-1" />

        {/* 版本历史下拉（§2.10，w-72） */}
        <Dropdown
          align="right"
          panelClassName="w-72 p-2"
          trigger={({ toggle }) => (
            <button
              type="button"
              onClick={toggle}
              aria-label={zh.document.versionHistory}
              title={zh.document.versionHistory}
              className={iconBtn}
            >
              <ClockIcon className="w-4 h-4" strokeWidth={1.5} />
            </button>
          )}
        >
          {() => (
            <VersionHistory
              versions={versions}
              onDownload={onVersionDownload}
              className="max-h-80 overflow-y-auto scrollbar-thin"
            />
          )}
        </Dropdown>

        {/* 下载下拉 */}
        <Dropdown
          align="right"
          panelClassName="w-auto px-3 py-2.5"
          trigger={({ toggle }) => (
            <button
              type="button"
              onClick={toggle}
              aria-label={zh.document.download}
              title={zh.document.download}
              className={iconBtn}
            >
              <ArrowDownTrayIcon className="w-4 h-4" strokeWidth={1.5} />
            </button>
          )}
        >
          {({ close }) => (
            <DownloadMenu
              disabled={!doc || !onDownload}
              onDownload={(format) => {
                onDownload?.(format)
                close()
              }}
            />
          )}
        </Dropdown>

        <button
          type="button"
          onClick={() => setFullscreen(true)}
          aria-label={zh.document.fullscreen}
          title={zh.document.fullscreen}
          className={iconBtn}
        >
          <ArrowsPointingOutIcon className="w-4 h-4" strokeWidth={1.5} />
        </button>

        {onToggleCollapse && (
          <button
            type="button"
            onClick={onToggleCollapse}
            aria-label={zh.document.collapse}
            title={zh.document.collapse}
            className={iconBtn}
          >
            <ChevronDoubleRightIcon className="w-4 h-4" strokeWidth={1.5} />
          </button>
        )}
      </div>

      {/* 流式渐变活动条 */}
      {doc?.streaming && (
        <div
          className="h-0.5 shrink-0 bg-gradient-to-r from-blue-500 via-purple-500 to-blue-500 animate-badge-shimmer bg-[length:200%_100%]"
          aria-hidden="true"
        />
      )}

      {/* 正文 */}
      <div className="flex-1 min-h-0 overflow-y-auto px-8 py-6">{body}</div>

      {/* 全屏 Modal */}
      <Modal
        open={fullscreen}
        onClose={() => setFullscreen(false)}
        title={doc?.filename ?? title}
        panelClassName="max-w-4xl h-[85vh] flex flex-col overflow-hidden"
      >
        <div className="flex-1 min-h-0 overflow-y-auto pr-1">
          {doc && <StreamingMarkdown markdown={doc.markdown} streaming={doc.streaming} />}
        </div>
      </Modal>
    </aside>
  )
}
