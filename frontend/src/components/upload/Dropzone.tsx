import { useRef, useState, type ReactNode } from 'react'
import { ArrowUpTrayIcon } from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'

export interface DropzoneProps {
  onFiles: (files: File[]) => void
  /** <input accept>；缺省不限。 */
  accept?: string
  multiple?: boolean
  /** 主提示（缺省「点击或拖放文件到此处」）。 */
  title?: string
  /** 类型 / 大小提示行。 */
  hint?: string
  disabled?: boolean
  className?: string
  children?: ReactNode
}

/**
 * §2.9 Dropzone：`rounded-2xl border-2 border-dashed … px-6 py-10` 上传区，
 * drag-over 蓝色高亮 + ring；点击打开文件选择。
 */
export function Dropzone({
  onFiles,
  accept,
  multiple = true,
  title = zh.upload.dropzoneTitle,
  hint = zh.upload.dropzoneHintDefault,
  disabled = false,
  className,
  children,
}: DropzoneProps) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [dragOver, setDragOver] = useState(false)

  const emit = (list: FileList | null) => {
    if (!list || list.length === 0) return
    onFiles(Array.from(list))
  }

  return (
    <div
      role="button"
      tabIndex={disabled ? -1 : 0}
      aria-disabled={disabled}
      aria-label={hint ? `${title}（${hint}）` : title}
      onClick={() => {
        if (!disabled) inputRef.current?.click()
      }}
      onKeyDown={(e) => {
        if (disabled) return
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          inputRef.current?.click()
        }
      }}
      onDragOver={(e) => {
        e.preventDefault()
        if (!disabled) setDragOver(true)
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={(e) => {
        e.preventDefault()
        setDragOver(false)
        if (!disabled) emit(e.dataTransfer.files)
      }}
      className={cn(
        'w-full rounded-2xl border-2 border-dashed px-6 py-10 flex flex-col items-center gap-3',
        'transition-colors cursor-pointer select-none outline-none',
        'focus-visible:ring-4 focus-visible:ring-blue-500/10',
        dragOver
          ? 'border-blue-500 bg-blue-50/50 dark:bg-blue-500/10 ring-4 ring-blue-500/10'
          : 'border-gray-300 dark:border-gray-600 bg-gray-50/50 dark:bg-gray-800/40 ' +
              'hover:border-[#6366f1]/50 hover:bg-blue-50/30 dark:hover:bg-blue-500/5',
        disabled && 'opacity-50 pointer-events-none',
        className,
      )}
    >
      <ArrowUpTrayIcon className="w-8 h-8 text-gray-400 dark:text-gray-500" strokeWidth={1.5} />
      <span className="text-sm font-medium text-gray-700 dark:text-gray-200">{title}</span>
      {hint && <span className="text-xs text-gray-400 dark:text-gray-500">{hint}</span>}
      {children}
      <input
        ref={inputRef}
        type="file"
        accept={accept}
        multiple={multiple}
        className="hidden"
        onChange={(e) => {
          emit(e.target.files)
          e.target.value = ''
        }}
      />
    </div>
  )
}
