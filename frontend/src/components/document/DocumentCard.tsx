import { useState } from 'react'
import { ArrowsPointingOutIcon, DocumentTextIcon } from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { Badge } from '../ui/Badge'
import { Card } from '../ui/Card'
import { Modal } from '../ui/Modal'
import { StreamingMarkdown } from '../markdown/StreamingMarkdown'
import type { PanelDoc } from './DocumentPanel'

export interface DocumentCardProps {
  title?: string
  doc: PanelDoc
  className?: string
}

/**
 * §3.2 流内 DocumentCard（xl 以下替代右侧面板）：
 * 限高 max-h-[420px] + 底部渐隐 + 「展开全文」Modal。
 */
export function DocumentCard({
  title = zh.document.panelTitle,
  doc,
  className,
}: DocumentCardProps) {
  const [expanded, setExpanded] = useState(false)

  return (
    <Card className={cn('overflow-hidden', className)}>
      {/* 头部 */}
      <div className="h-11 px-4 flex items-center gap-2 border-b border-gray-100 dark:border-gray-700">
        <DocumentTextIcon className="w-4 h-4 shrink-0 text-gray-400" strokeWidth={1.5} />
        <span className="text-sm font-semibold text-gray-900 dark:text-gray-100 truncate">
          {title}
        </span>
        {doc.version !== undefined && (
          <Badge variant="gradient" className="shrink-0">
            {zh.document.versionBadge(doc.version)}
          </Badge>
        )}
        {doc.streaming && (
          <span className="flex items-center gap-1 text-[10px] font-medium text-[#6366f1] dark:text-indigo-400 shrink-0">
            <span className="w-1.5 h-1.5 rounded-full bg-[#6366f1] animate-pulse" />
            {zh.document.streamingBadge}
          </span>
        )}
        <div className="flex-1" />
        <button
          type="button"
          onClick={() => setExpanded(true)}
          className={cn(
            'flex items-center gap-1 px-2 py-1 rounded-lg text-xs font-medium',
            'text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700',
            'hover:text-gray-700 dark:hover:text-gray-200 transition-colors',
          )}
        >
          <ArrowsPointingOutIcon className="w-3.5 h-3.5" strokeWidth={1.5} />
          {zh.document.expandFull}
        </button>
      </div>

      {doc.streaming && (
        <div
          className="h-0.5 bg-gradient-to-r from-blue-500 via-purple-500 to-blue-500 animate-badge-shimmer bg-[length:200%_100%]"
          aria-hidden="true"
        />
      )}

      {/* 限高正文 + 底部渐隐 */}
      <div className="relative max-h-[420px] overflow-hidden">
        <div className="px-5 py-4">
          <StreamingMarkdown markdown={doc.markdown} streaming={doc.streaming} />
        </div>
        <div
          className="pointer-events-none absolute inset-x-0 bottom-0 h-16 bg-gradient-to-t from-white dark:from-gray-800 to-transparent"
          aria-hidden="true"
        />
      </div>

      {/* 展开全文 Modal */}
      <Modal
        open={expanded}
        onClose={() => setExpanded(false)}
        title={doc.filename ?? title}
        panelClassName="max-w-3xl h-[85vh] flex flex-col overflow-hidden"
      >
        <div className="flex-1 min-h-0 overflow-y-auto pr-1">
          <StreamingMarkdown markdown={doc.markdown} streaming={doc.streaming} />
        </div>
      </Modal>
    </Card>
  )
}
