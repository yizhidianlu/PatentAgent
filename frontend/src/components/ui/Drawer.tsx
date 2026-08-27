import { useEffect, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { XMarkIcon } from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'

export interface DrawerProps {
  open: boolean
  onClose: () => void
  title?: string
  panelClassName?: string
  children: ReactNode
}

/** §2.6 Drawer：右侧 `w-full sm:w-[480px] shadow-xl border-l` slide-in-from-right duration-300 + scrim。 */
export function Drawer({ open, onClose, title, panelClassName, children }: DrawerProps) {
  useEffect(() => {
    if (!open) return
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [open, onClose])

  if (!open) return null

  return createPortal(
    <div className="fixed inset-0 z-[var(--z-drawer)]" role="dialog" aria-modal="true" aria-label={title}>
      <div
        className="absolute inset-0 bg-black/40 backdrop-blur-sm animate-in fade-in duration-200"
        onClick={onClose}
      />
      <div
        className={cn(
          'absolute inset-y-0 right-0 w-full sm:w-[480px] bg-white dark:bg-gray-900 shadow-xl ' +
            'border-l border-gray-200 dark:border-gray-700 flex flex-col ' +
            'animate-in slide-in-from-right duration-300',
          panelClassName,
        )}
      >
        <div className="flex items-center justify-between px-5 h-14 border-b border-gray-200 dark:border-gray-700 shrink-0">
          <h2 className="text-sm font-semibold text-gray-900 dark:text-gray-100 truncate">{title}</h2>
          <button
            type="button"
            onClick={onClose}
            aria-label={zh.common.close}
            className="w-8 h-8 rounded-full flex items-center justify-center text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800 hover:text-gray-600 dark:hover:text-gray-200 transition-colors"
          >
            <XMarkIcon className="w-5 h-5" strokeWidth={1.5} />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto p-5">{children}</div>
      </div>
    </div>,
    document.body,
  )
}
