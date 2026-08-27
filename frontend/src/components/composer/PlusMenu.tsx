import { useEffect, useRef, useState } from 'react'
import { DocumentArrowUpIcon, PhotoIcon, PlusIcon } from '@heroicons/react/24/outline'
import { zh } from '../../i18n/zh'
import { cn } from '../../lib/cn'

export interface PlusMenuProps {
  onPickFile: () => void
  onPickImage: () => void
  disabled?: boolean
}

/**
 * §2.3 PlusMenu：+ 钮（打开态 bg-gray-200 rotate-45）+ 向上弹出菜单
 * `bottom-full mb-2 left-0 w-56 rounded-xl shadow-lg border py-1.5`。
 */
export function PlusMenu({ onPickFile, onPickImage, disabled = false }: PlusMenuProps) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onPointerDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false)
    }
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('mousedown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  const pick = (fn: () => void) => {
    setOpen(false)
    fn()
  }

  const itemCls =
    'w-full flex items-center gap-2.5 px-3 py-2 text-sm text-gray-700 dark:text-gray-200 ' +
    'hover:bg-gray-50 dark:hover:bg-gray-700/60 transition-colors'

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        aria-label={zh.composer.attachLabel}
        aria-expanded={open}
        disabled={disabled}
        onClick={() => setOpen((v) => !v)}
        className={cn(
          'w-8 h-8 rounded-full flex items-center justify-center text-gray-500 dark:text-gray-400',
          'transition-all duration-200 disabled:opacity-50 disabled:pointer-events-none',
          open
            ? 'bg-gray-200 dark:bg-gray-700 rotate-45'
            : 'hover:bg-gray-100 dark:hover:bg-gray-700',
        )}
      >
        <PlusIcon className="w-5 h-5" strokeWidth={1.5} />
      </button>

      {open && (
        <div
          role="menu"
          className={cn(
            'absolute bottom-full mb-2 left-0 w-56 bg-white dark:bg-gray-800 rounded-xl shadow-lg',
            'border border-gray-200 dark:border-gray-700 py-1.5 z-30',
            'animate-in fade-in slide-in-from-bottom-2 duration-200',
          )}
        >
          <button type="button" role="menuitem" className={itemCls} onClick={() => pick(onPickFile)}>
            <DocumentArrowUpIcon className="w-4 h-4 text-gray-400" strokeWidth={1.5} />
            {zh.composer.uploadFile}
          </button>
          <button type="button" role="menuitem" className={itemCls} onClick={() => pick(onPickImage)}>
            <PhotoIcon className="w-4 h-4 text-gray-400" strokeWidth={1.5} />
            {zh.composer.uploadImage}
          </button>
        </div>
      )}
    </div>
  )
}
