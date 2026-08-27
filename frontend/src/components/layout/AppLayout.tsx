import { useRef, type UIEvent } from 'react'
import { Outlet } from 'react-router-dom'
import { motion } from 'framer-motion'
import { useUiStore } from '../../stores/uiStore'
import { AppHeader } from './AppHeader'
import { Sidebar, useSidebarWidth } from './Sidebar'
import { MobileDrawer } from './MobileDrawer'
import { MAIN_CONTENT_ID, SkipToContent } from './SkipToContent'

const HIDE_SCROLL_THRESHOLD = 80

/**
 * §3 AppLayout：`h-screen flex flex-col overflow-hidden` → AppHeader →
 * `flex flex-1 overflow-hidden relative` → Sidebar + main（滚动容器，驱动 header 自动隐藏）。
 * 主内容 padding-left 与侧栏宽度同步动画（§2.2）。
 */
export function AppLayout() {
  const setHeaderHidden = useUiStore((s) => s.setHeaderHidden)
  const sidebarWidth = useSidebarWidth()
  const lastScrollY = useRef(0)

  const onMainScroll = (e: UIEvent<HTMLElement>) => {
    const y = e.currentTarget.scrollTop
    const last = lastScrollY.current
    if (y <= 0) {
      // 到顶显示
      setHeaderHidden(false)
    } else if (y > last && y > HIDE_SCROLL_THRESHOLD) {
      // 下滑 >80px 隐藏
      setHeaderHidden(true)
    } else if (y < last) {
      // 任何上滑显示
      setHeaderHidden(false)
    }
    lastScrollY.current = y
  }

  return (
    <div className="h-screen bg-white dark:bg-gray-900 flex flex-col overflow-hidden">
      <SkipToContent />
      <AppHeader />
      <div className="flex flex-1 overflow-hidden relative">
        <Sidebar />
        <motion.main
          id={MAIN_CONTENT_ID}
          tabIndex={-1}
          animate={{ paddingLeft: sidebarWidth }}
          initial={false}
          transition={{ duration: 0.3, ease: [0.25, 0.1, 0.25, 1] }}
          onScroll={onMainScroll}
          className="flex-1 overflow-y-auto flex flex-col outline-none"
        >
          <Outlet />
        </motion.main>
      </div>
      <MobileDrawer />
    </div>
  )
}
