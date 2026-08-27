import { useEffect } from 'react'
import { createPortal } from 'react-dom'
import {
  CheckCircleIcon,
  ExclamationTriangleIcon,
  InformationCircleIcon,
  XCircleIcon,
  XMarkIcon,
} from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { useUiStore, type ToastItem } from '../../stores/uiStore'

const TOAST_DURATION_MS = 4000

const kindIcon = {
  success: { Icon: CheckCircleIcon, cls: 'text-emerald-500' },
  warning: { Icon: ExclamationTriangleIcon, cls: 'text-amber-500' },
  error: { Icon: XCircleIcon, cls: 'text-red-500' },
  info: { Icon: InformationCircleIcon, cls: 'text-[#6366f1]' },
} as const

function ToastCard({ toast }: { toast: ToastItem }) {
  const dismissToast = useUiStore((s) => s.dismissToast)

  useEffect(() => {
    const timer = setTimeout(() => dismissToast(toast.id), TOAST_DURATION_MS)
    return () => clearTimeout(timer)
  }, [toast.id, dismissToast])

  const { Icon, cls } = kindIcon[toast.kind]

  return (
    <div
      role="status"
      className={cn(
        'pointer-events-auto flex items-start gap-2.5 w-80 max-w-[calc(100vw-2rem)] ' +
          'rounded-xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 ' +
          'shadow-xl px-4 py-3 text-sm text-gray-700 dark:text-gray-200 ' +
          'animate-in slide-in-from-bottom-2 duration-200',
      )}
    >
      <Icon className={cn('w-5 h-5 shrink-0 mt-px', cls)} strokeWidth={1.5} />
      <p className="flex-1 min-w-0 break-words">{toast.message}</p>
      <button
        type="button"
        onClick={() => dismissToast(toast.id)}
        aria-label={zh.common.close}
        className="shrink-0 -mr-1 w-5 h-5 rounded flex items-center justify-center text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 transition-colors"
      >
        <XMarkIcon className="w-4 h-4" strokeWidth={1.5} />
      </button>
    </div>
  )
}

/** 右下角 Toast viewport（挂在 App 根部一次）。 */
export function ToastViewport() {
  const toasts = useUiStore((s) => s.toasts)
  if (toasts.length === 0) return null
  return createPortal(
    <div className="pointer-events-none fixed bottom-4 right-4 z-[var(--z-toast)] flex flex-col items-end gap-2">
      {toasts.map((t) => (
        <ToastCard key={t.id} toast={t} />
      ))}
    </div>,
    document.body,
  )
}
