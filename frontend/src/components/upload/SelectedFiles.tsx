import { DocumentTextIcon, XMarkIcon } from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { formatFileSize } from '../../lib/format'

export interface SelectedFileItem {
  name: string
  size?: number | null
}

export interface SelectedFilesProps {
  files: SelectedFileItem[]
  /** 给了才渲染删除按钮；不给表示这批文件已入库、不可在此处移除。 */
  onRemove?: (index: number) => void
  /** 列表标题，默认「已选择 N 个文件」。 */
  title?: string
  className?: string
}

/**
 * 已选文件列表。
 *
 * 存在的理由是一次真实的困惑：导入弹窗里选完文件只渲染一行裸文本文件名，
 * 与下方的表单标签长得一模一样，用户看不出到底选上没有——「好像没上传一样」。
 * 选中是一个状态变化，得有与之匹配的视觉重量：计数、图标块、边框、文件大小。
 *
 * 顺带解决另一半问题：选错了没法取消，只能关掉弹窗重来。
 */
export function SelectedFiles({ files, onRemove, title, className }: SelectedFilesProps) {
  if (files.length === 0) return null
  return (
    <div className={cn('space-y-1.5', className)}>
      <p className="text-xs font-medium text-indigo-600 dark:text-indigo-400">
        {title ?? `已选择 ${files.length} 个文件`}
      </p>
      {files.map((file, index) => (
        <div
          key={`${file.name}-${index}`}
          className="flex items-center gap-2.5 rounded-lg border border-gray-200 dark:border-gray-700
                     bg-gray-50/60 dark:bg-gray-800/40 px-3 py-2"
        >
          <span className="w-7 h-7 rounded bg-white dark:bg-gray-700 flex items-center justify-center shrink-0">
            <DocumentTextIcon className="w-4 h-4 text-indigo-500" strokeWidth={1.5} />
          </span>
          <span
            className="text-xs font-medium text-gray-800 dark:text-gray-100 truncate flex-1 min-w-0"
            title={file.name}
          >
            {file.name}
          </span>
          {typeof file.size === 'number' && (
            <span className="text-[10px] text-gray-400 shrink-0 tabular-nums">
              {formatFileSize(file.size)}
            </span>
          )}
          {onRemove && (
            <button
              type="button"
              onClick={() => onRemove(index)}
              aria-label={`移除 ${file.name}`}
              className="shrink-0 w-5 h-5 rounded flex items-center justify-center
                         text-gray-400 hover:text-red-500 hover:bg-red-50
                         dark:hover:bg-red-500/10 transition-colors"
            >
              <XMarkIcon className="w-3.5 h-3.5" strokeWidth={2} />
            </button>
          )}
        </div>
      ))}
    </div>
  )
}
