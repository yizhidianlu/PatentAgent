import { useEffect, useRef, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { XMarkIcon } from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'

export interface ModalProps {
  open: boolean
  onClose: () => void
  title?: string
  /** 面板附加类名（默认 max-w-lg p-6）。 */
  panelClassName?: string
  children: ReactNode
}

const FOCUSABLE =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'

/** §2.6 Modal：黑半透明 blur overlay + rounded-2xl 面板 + 焦点陷阱 + Esc 关闭。 */
export function Modal({ open, onClose, title, panelClassName, children }: ModalProps) {
  const panelRef = useRef<HTMLDivElement>(null)
  const lastActiveRef = useRef<Element | null>(null)

  // onClose 常被父组件写成内联箭头函数，每次重渲染都是新引用。若把它放进下面
  // 主 effect 的依赖里，用户每敲一个字（父组件 setState → 重渲染）就会触发
  // effect 重跑、把焦点抢回面板第一个可聚焦元素（通常是关闭按钮），表单根本
  // 没法连续输入。故用 ref 承接，主 effect 只依赖 open。
  const onCloseRef = useRef(onClose)
  useEffect(() => {
    onCloseRef.current = onClose
  }, [onClose])

  useEffect(() => {
    if (!open) return
    lastActiveRef.current = document.activeElement

    const panel = panelRef.current
    // 初始焦点优先给第一个输入控件——弹窗多为表单，落在关闭按钮上既不便也易误触
    const firstField = panel?.querySelector<HTMLElement>(
      'input:not([disabled]):not([type="hidden"]), textarea:not([disabled]), select:not([disabled])',
    )
    const first = firstField ?? panel?.querySelector<HTMLElement>(FOCUSABLE)
    ;(first ?? panel)?.focus()

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation()
        onCloseRef.current()
        return
      }
      if (e.key !== 'Tab' || !panelRef.current) return
      // 焦点陷阱：Tab 循环限制在面板内
      const nodes = Array.from(panelRef.current.querySelectorAll<HTMLElement>(FOCUSABLE))
      if (nodes.length === 0) {
        e.preventDefault()
        return
      }
      const firstNode = nodes[0]
      const lastNode = nodes[nodes.length - 1]
      const active = document.activeElement
      if (e.shiftKey && (active === firstNode || active === panelRef.current)) {
        e.preventDefault()
        lastNode.focus()
      } else if (!e.shiftKey && active === lastNode) {
        e.preventDefault()
        firstNode.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown, true)
    return () => {
      document.removeEventListener('keydown', onKeyDown, true)
      if (lastActiveRef.current instanceof HTMLElement) lastActiveRef.current.focus()
    }
  }, [open])

  if (!open) return null

  return createPortal(
    <div
      className="fixed inset-0 z-[var(--z-modal)] flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-label={title}
    >
      <div
        className="absolute inset-0 bg-black/50 backdrop-blur-sm animate-in fade-in duration-200"
        onClick={onClose}
      />
      <div
        ref={panelRef}
        tabIndex={-1}
        className={cn(
          'relative w-full max-w-lg rounded-2xl bg-white dark:bg-gray-800 shadow-xl p-6 ' +
            'animate-in fade-in zoom-in-95 duration-200 outline-none',
          panelClassName,
        )}
      >
        <div className={cn('flex items-start justify-between gap-4', title && 'mb-4')}>
          {title && (
            <h2 className="text-base font-semibold text-gray-900 dark:text-gray-100">{title}</h2>
          )}
          <button
            type="button"
            onClick={onClose}
            aria-label={zh.common.close}
            className={cn(
              'w-8 h-8 -mr-2 -mt-2 rounded-full flex items-center justify-center text-gray-400 ' +
                'hover:bg-gray-100 dark:hover:bg-gray-700 hover:text-gray-600 dark:hover:text-gray-200 transition-colors',
              !title && 'absolute right-4 top-4 m-0',
            )}
          >
            <XMarkIcon className="w-5 h-5" strokeWidth={1.5} />
          </button>
        </div>
        {children}
      </div>
    </div>,
    document.body,
  )
}
