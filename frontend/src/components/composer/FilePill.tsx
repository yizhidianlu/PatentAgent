import { useEffect, useMemo } from 'react'
import {
  ArrowPathIcon,
  DocumentIcon,
  DocumentTextIcon,
  PaperClipIcon,
  PhotoIcon,
  PresentationChartBarIcon,
  XMarkIcon,
} from '@heroicons/react/24/outline'
import { zh } from '../../i18n/zh'
import { cn } from '../../lib/cn'
import { formatFileSize } from '../../lib/format'
import type { AttachmentKind, ComposerAttachment } from '../../stores/composerStore'

export interface FilePillProps {
  attachment: ComposerAttachment
  onRemove: () => void
  onRetry: () => void
}

const kindIcon: Record<
  AttachmentKind,
  { Icon: typeof DocumentIcon; cls: string }
> = {
  pdf: { Icon: DocumentTextIcon, cls: 'text-red-500' },
  word: { Icon: DocumentTextIcon, cls: 'text-blue-500' },
  ppt: { Icon: PresentationChartBarIcon, cls: 'text-orange-500' },
  markdown: { Icon: DocumentIcon, cls: 'text-gray-500 dark:text-gray-400' },
  text: { Icon: DocumentIcon, cls: 'text-gray-500 dark:text-gray-400' },
  image: { Icon: PhotoIcon, cls: 'text-emerald-500' },
  other: { Icon: PaperClipIcon, cls: 'text-gray-500 dark:text-gray-400' },
}

/**
 * §2.3 FilePill：附件胶囊 —— 类型图标 / 图片缩略图、名称+大小、
 * 上传进度条、错误红边+重试、hover 显示移除钮。
 */
export function FilePill({ attachment, onRemove, onRetry }: FilePillProps) {
  const isImage = attachment.kind === 'image'
  const thumbUrl = useMemo(
    () => (isImage ? URL.createObjectURL(attachment.file) : null),
    [isImage, attachment.file],
  )
  useEffect(
    () => () => {
      if (thumbUrl) URL.revokeObjectURL(thumbUrl)
    },
    [thumbUrl],
  )

  const { Icon, cls } = kindIcon[attachment.kind]
  const uploading = attachment.status === 'uploading'
  const failed = attachment.status === 'error'

  return (
    <div
      className={cn(
        'group relative flex items-center gap-2 bg-gray-100 dark:bg-gray-800 p-2 rounded-lg border',
        failed
          ? 'border-red-300 dark:border-red-500/50'
          : 'border-gray-200 dark:border-gray-700',
      )}
    >
      {thumbUrl ? (
        <img
          src={thumbUrl}
          alt={attachment.name}
          className="w-8 h-8 rounded object-cover shrink-0"
        />
      ) : (
        <span className="w-8 h-8 rounded bg-gray-200 dark:bg-gray-700 flex items-center justify-center shrink-0">
          <Icon className={cn('w-4 h-4', cls)} strokeWidth={1.5} />
        </span>
      )}

      <div className="flex-1 min-w-0">
        <p className="text-xs font-medium text-gray-800 dark:text-gray-100 truncate">
          {attachment.name}
        </p>
        <div className="flex items-center gap-1.5">
          <span className="text-[10px] text-gray-500 dark:text-gray-400">
            {formatFileSize(attachment.size)}
          </span>
          {uploading && (
            <span className="text-[10px] text-gray-400">{zh.composer.uploading}</span>
          )}
          {failed && (
            <button
              type="button"
              onClick={onRetry}
              aria-label={`${zh.composer.retryUpload}：${attachment.name}`}
              className="inline-flex items-center gap-0.5 text-[10px] font-medium text-red-500 hover:text-red-600"
            >
              <ArrowPathIcon className="w-3 h-3" strokeWidth={1.5} />
              {zh.common.retry}
            </button>
          )}
        </div>
        {/* 失败原因（具体文案而非「出错了」；过长时 tooltip 看全文） */}
        {failed && (
          <p
            title={attachment.error ?? zh.composer.uploadError}
            className="mt-0.5 text-[10px] leading-snug text-red-500 dark:text-red-400 line-clamp-2 break-words"
          >
            {attachment.error ?? zh.composer.uploadError}
          </p>
        )}
        {uploading && (
          <div className="h-1 mt-1 rounded-full bg-gray-200 dark:bg-gray-600 overflow-hidden">
            <div
              className="h-full rounded-full bg-[#6366f1] transition-[width] duration-200"
              style={{ width: `${attachment.progress}%` }}
            />
          </div>
        )}
      </div>

      <button
        type="button"
        aria-label={zh.composer.remove}
        onClick={onRemove}
        className={cn(
          'absolute -top-1.5 -right-1.5 w-4 h-4 rounded-full bg-gray-500 hover:bg-gray-600 text-white',
          'opacity-0 group-hover:opacity-100 focus-visible:opacity-100 transition-opacity',
          'flex items-center justify-center',
        )}
      >
        <XMarkIcon className="w-3 h-3" strokeWidth={2} />
      </button>
    </div>
  )
}
