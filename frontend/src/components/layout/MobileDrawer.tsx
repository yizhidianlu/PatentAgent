import { useCallback, useEffect } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { XMarkIcon } from '@heroicons/react/24/outline'
import { zh } from '../../i18n/zh'
import { useUiStore } from '../../stores/uiStore'
import { SidebarExpandedContent } from './Sidebar'

/** §2.2 MobileDrawer（<768px）：scrim + 左侧 280px 面板，x:'-100%'→0 .3s。 */
export function MobileDrawer() {
  const open = useUiStore((s) => s.mobileDrawerOpen)
  const setOpen = useUiStore((s) => s.setMobileDrawerOpen)
  const close = useCallback(() => setOpen(false), [setOpen])

  /*
   * Esc 关闭：与 ui/Modal、ui/Drawer 保持同一套关闭手势。
   * 缺了这条时抽屉只能靠点 scrim 或 × 关，而它是 fixed inset-0 覆盖全屏——
   * 键盘用户一旦打开就没有退路，后面的页面也全部点不中。
   */
  useEffect(() => {
    if (!open) return
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') close()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [open, close])

  return (
    <AnimatePresence>
      {open && (
        <div
          className="md:hidden fixed inset-0 z-[55]"
          role="dialog"
          aria-modal="true"
          aria-label={zh.sidebar.navLabel}
        >
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="absolute inset-0 bg-black/40 backdrop-blur-sm"
            onClick={close}
          />
          <motion.div
            initial={{ x: '-100%' }}
            animate={{ x: 0 }}
            exit={{ x: '-100%' }}
            transition={{ duration: 0.3, ease: [0.25, 0.1, 0.25, 1] }}
            className="absolute inset-y-0 left-0 w-[280px] bg-gray-50 dark:bg-gray-900 border-r border-gray-100 dark:border-gray-800 flex flex-col overflow-hidden"
          >
            <div className="flex items-center justify-between pl-4 pr-2 h-[var(--app-header-h)] border-b border-gray-100 dark:border-gray-800 shrink-0">
              <span className="text-sm font-bold text-gray-900 dark:text-white truncate">
                {zh.brand.name}
              </span>
              <button
                type="button"
                onClick={close}
                aria-label={zh.sidebar.closeMenu}
                className="w-8 h-8 rounded-full flex items-center justify-center text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors"
              >
                <XMarkIcon className="w-5 h-5" strokeWidth={1.5} />
              </button>
            </div>
            <SidebarExpandedContent onNavigate={close} />
          </motion.div>
        </div>
      )}
    </AnimatePresence>
  )
}
