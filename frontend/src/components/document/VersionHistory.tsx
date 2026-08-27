import { cn } from '../../lib/cn'
import { formatDateTime } from '../../lib/format'
import { zh } from '../../i18n/zh'
import type { IterationType } from '../../types/models'
import { Badge } from '../ui/Badge'
import { EmptyState } from '../ui/EmptyState'
import { DownloadMenu } from './DownloadMenu'
import type { DownloadFormat } from '../../lib/download'

/** 版本历史行数据（数据经 props 传入；M4 接 useVersions）。 */
export interface VersionEntry {
  id: string
  version: number
  filename: string
  createdAt: string
  iterationType?: IterationType | null
  /**
   * 该版本实际存在的下载格式（缺省 = 全部三种）。
   * 传了就只渲染这几个 chip：论文转专利交付的是 JSON/DOCX/PDF、审查答复只有 MD/DOCX，
   * 无差别摆出 MD/DOCX/PDF 会让用户点到一个必然只弹「尚未生成」的钮。
   */
  formats?: readonly DownloadFormat[]
}

export interface VersionHistoryProps {
  /** 由新到旧排序；首行视为当前版本。 */
  versions: VersionEntry[]
  onDownload?: (entry: VersionEntry, format: DownloadFormat) => void
  className?: string
}

/**
 * §2.10 VersionHistory：行 `flex justify-between rounded-xl border px-4 py-3 hover:bg-gray-50`；
 * 左 V 徽章(gradient) + 文件名 + `2026-08-25 14:32 · 纠正迭代`；最新行「当前」emerald 徽章；
 * 右 MD/DOCX/PDF chip 钮。
 */
export function VersionHistory({ versions, onDownload, className }: VersionHistoryProps) {
  if (versions.length === 0) {
    return <EmptyState title={zh.document.versionEmpty} className="py-6" />
  }

  return (
    <div className={cn('space-y-2', className)}>
      {versions.map((entry, index) => {
        const isCurrent = index === 0
        const iterationLabel = entry.iterationType
          ? zh.document.iterationLabels[entry.iterationType]
          : null
        return (
          <div
            key={entry.id}
            className={cn(
              'flex items-center justify-between gap-3 rounded-xl border px-4 py-3',
              'border-gray-200 dark:border-gray-700',
              'hover:bg-gray-50 dark:hover:bg-gray-700/40 transition-colors',
            )}
          >
            <div className="flex items-center gap-2.5 min-w-0">
              <Badge variant="gradient" className="shrink-0">
                {zh.document.versionBadge(entry.version)}
              </Badge>
              <div className="min-w-0">
                <div className="flex items-center gap-1.5 min-w-0">
                  <span className="text-sm text-gray-900 dark:text-gray-100 truncate">
                    {entry.filename}
                  </span>
                  {isCurrent && (
                    <Badge variant="emerald" className="shrink-0">
                      {zh.document.current}
                    </Badge>
                  )}
                </div>
                <p className="text-xs text-gray-500 dark:text-gray-400">
                  {formatDateTime(entry.createdAt)}
                  {iterationLabel ? ` · ${iterationLabel}` : ''}
                </p>
              </div>
            </div>
            {onDownload && entry.formats?.length !== 0 && (
              <DownloadMenu
                className="shrink-0"
                formats={entry.formats}
                onDownload={(format) => onDownload(entry, format)}
              />
            )}
          </div>
        )
      })}
    </div>
  )
}
