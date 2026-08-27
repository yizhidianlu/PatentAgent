import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import type { DownloadFormat } from '../../lib/download'

export interface DownloadMenuProps {
  onDownload: (format: DownloadFormat) => void
  /** 展示哪些格式（默认全部）。 */
  formats?: readonly DownloadFormat[]
  disabled?: boolean
  className?: string
}

const ALL_FORMATS: readonly DownloadFormat[] = ['md', 'docx', 'pdf']

/**
 * §2.10 DownloadMenu：MD / DOCX / PDF chip 钮（onClick 走下载工具）。
 * 嵌于 DocumentPanel 头部下拉、VersionHistory 行与 DeliveryCard。
 */
export function DownloadMenu({
  onDownload,
  formats = ALL_FORMATS,
  disabled = false,
  className,
}: DownloadMenuProps) {
  return (
    <div className={cn('flex items-center gap-1.5', className)}>
      {formats.map((format) => (
        <button
          key={format}
          type="button"
          disabled={disabled}
          onClick={() => onDownload(format)}
          className={cn(
            'px-2 py-1 rounded-md border border-gray-200 dark:border-gray-600',
            'text-[11px] font-medium text-gray-600 dark:text-gray-300',
            'bg-white dark:bg-gray-800 transition-colors',
            'hover:border-indigo-400/60 hover:text-[#6366f1] dark:hover:text-indigo-400',
            'disabled:opacity-50 disabled:pointer-events-none',
          )}
        >
          {zh.document.formats[format]}
        </button>
      ))}
    </div>
  )
}
