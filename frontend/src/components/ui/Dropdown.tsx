import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { createPortal } from 'react-dom'
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
  const [coords, setCoords] = useState<{ top: number; left: number } | null>(null)
  const rootRef = useRef<HTMLDivElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)

  const close = useCallback(() => {
    setOpen(false)
    setFlipped(false)
    setCoords(null)
  }, [])
  const toggle = useCallback(() => {
    setOpen((v) => {
      if (v) {
        setFlipped(false)
        setCoords(null)
      }
      return !v
    })
  }, [])

  /**
   * 面板改用 portal + fixed 定位。
   *
   * 起因：面板原先是 `absolute`，会被任何设了 overflow 的祖先裁切 —— 管理后台
   * 用户列表就同时套着 `Card.overflow-hidden` 与表格的 `overflow-x-auto`，
   * 行操作菜单一展开就被切掉半截，得滚动才能看全。改到 body 下用视口坐标定位
   * 后，无论外层怎么裁切都不受影响；下方空间不足时仍自动向上翻转。
   */
  useLayoutEffect(() => {
    if (!open) return

    const place = () => {
      const panel = panelRef.current
      const root = rootRef.current
      if (!panel || !root) return
      const t = root.getBoundingClientRect()
      const ph = panel.offsetHeight
      const pw = panel.offsetWidth
      const margin = 8

      // 上下：优先按 placement，空间不足且反向够用时翻转
      let toTop = placement === 'top'
      if (placement === 'bottom') {
        const overflowBottom = t.bottom + margin + ph > window.innerHeight
        if (overflowBottom && t.top - margin - ph > 0) toTop = true
      } else {
        const overflowTop = t.top - margin - ph < 0
        if (overflowTop && t.bottom + margin + ph < window.innerHeight) toTop = false
      }
      setFlipped(toTop !== (placement === 'top'))

      const top = toTop ? t.top - margin - ph : t.bottom + margin
      // 左右：按 align 贴边，再钳进视口，避免右侧菜单在窄屏溢出
      let left = align === 'right' ? t.right - pw : t.left
      left = Math.min(Math.max(margin, left), Math.max(margin, window.innerWidth - pw - margin))
      setCoords({ top, left })
    }

    place()
    // 滚动/缩放时跟随（capture 以便捕获内部滚动容器）
    window.addEventListener('scroll', place, true)
    window.addEventListener('resize', place)
    return () => {
      window.removeEventListener('scroll', place, true)
      window.removeEventListener('resize', place)
    }
  }, [open, placement, align])

  const effectivePlacement = flipped
    ? placement === 'bottom'
      ? 'top'
      : 'bottom'
    : placement

  useEffect(() => {
    if (!open) return
    const onPointerDown = (e: PointerEvent) => {
      const target = e.target as Node
      const inTrigger = rootRef.current?.contains(target)
      const inPanel = panelRef.current?.contains(target)
      if (!inTrigger && !inPanel) close()
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
      {createPortal(
        <AnimatePresence>
          {open && (
            <motion.div
              ref={panelRef}
              initial={{ opacity: 0, y: effectivePlacement === 'bottom' ? 10 : -10, scale: 0.95 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: effectivePlacement === 'bottom' ? 10 : -10, scale: 0.95 }}
              transition={{ duration: 0.2 }}
              style={{
                top: coords?.top ?? -9999,
                left: coords?.left ?? -9999,
                // 首帧还没量到尺寸时先藏起来，避免在左上角闪一下
                visibility: coords ? 'visible' : 'hidden',
              }}
              className={cn(
                'fixed z-[var(--z-dropdown)] w-64 bg-white dark:bg-gray-800 rounded-xl shadow-xl ' +
                  'border border-gray-200 dark:border-gray-700 py-1.5',
                panelClassName,
              )}
            >
              {children({ close })}
            </motion.div>
          )}
        </AnimatePresence>,
        document.body,
      )}
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
