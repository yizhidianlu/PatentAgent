import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { cn } from '../../lib/cn'

export interface DropdownProps {
  /** 触发器渲染函数（open 状态 + 切换回调）。 */
  trigger: (props: { open: boolean; toggle: () => void }) => ReactNode
  /** 面板内容渲染函数（close 回调供菜单项调用）。 */
  children: (props: { close: () => void }) => ReactNode
  /** 面板对齐：触发器左缘 / 右缘。 */
  align?: 'left' | 'right'
  /** 面板放置：触发器下方 / 上方。 */
  placement?: 'bottom' | 'top'
  /** 面板附加类名（默认 w-64）。 */
  panelClassName?: string
  className?: string
}

/**
 * 通用 popper 下拉（§2.1 下拉面板规格）：
 * `w-64 bg-white dark:bg-gray-800 rounded-xl shadow-xl border py-1.5 z-[60]`，
 * motion {opacity:0,y:10,scale:.95}→{1,0,1} .2s + AnimatePresence；
 * 点击外部 / Esc 关闭。
 */
export function Dropdown({
  trigger,
  children,
  align = 'right',
  placement = 'bottom',
  panelClassName,
  className,
}: DropdownProps) {
  const [open, setOpen] = useState(false)
  const [flipped, setFlipped] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)

  const close = useCallback(() => {
    setOpen(false)
    setFlipped(false)
  }, [])
  const toggle = useCallback(() => {
    setOpen((v) => {
      if (v) setFlipped(false)
      return !v
    })
  }, [])

  /**
   * 视口翻转：面板打开后若下缘越出视口（如侧栏底部的会话行、页面底部的按钮），
   * 自动改为向上展开；上方空间也不够时维持原方向（此时页面本身可滚动）。
   */
  useLayoutEffect(() => {
    if (!open) return
    const panel = panelRef.current
    const root = rootRef.current
    if (!panel || !root) return
    const p = panel.getBoundingClientRect()
    const t = root.getBoundingClientRect()
    const margin = 8
    if (placement === 'bottom') {
      if (p.bottom > window.innerHeight - margin && t.top - p.height - margin > 0) setFlipped(true)
    } else if (p.top < margin && t.bottom + p.height + margin < window.innerHeight) {
      setFlipped(true)
    }
  }, [open, placement])

  const effectivePlacement = flipped
    ? placement === 'bottom'
      ? 'top'
      : 'bottom'
    : placement

  useEffect(() => {
    if (!open) return
    const onPointerDown = (e: PointerEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) close()
    }
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') close()
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open, close])

  return (
    <div ref={rootRef} className={cn('relative inline-block', className)}>
      {trigger({ open, toggle })}
      <AnimatePresence>
        {open && (
          <motion.div
            ref={panelRef}
            initial={{ opacity: 0, y: effectivePlacement === 'bottom' ? 10 : -10, scale: 0.95 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: effectivePlacement === 'bottom' ? 10 : -10, scale: 0.95 }}
            transition={{ duration: 0.2 }}
            className={cn(
              'absolute z-[60] w-64 bg-white dark:bg-gray-800 rounded-xl shadow-xl ' +
                'border border-gray-200 dark:border-gray-700 py-1.5',
              effectivePlacement === 'bottom' ? 'top-full mt-2' : 'bottom-full mb-2',
              align === 'right' ? 'right-0' : 'left-0',
              panelClassName,
            )}
          >
            {children({ close })}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

export interface DropdownItemProps {
  onClick?: () => void
  disabled?: boolean
  danger?: boolean
  className?: string
  children: ReactNode
}

/** 标准菜单行。 */
export function DropdownItem({ onClick, disabled, danger, className, children }: DropdownItemProps) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className={cn(
        'flex w-full items-center gap-2.5 px-3.5 py-2 text-sm text-left transition-colors',
        danger
          ? 'text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-500/10'
          : 'text-gray-700 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-700',
        disabled && 'opacity-50 pointer-events-none',
        className,
      )}
    >
      {children}
    </button>
  )
}
