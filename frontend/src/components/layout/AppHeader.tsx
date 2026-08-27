import { motion } from 'framer-motion'
import { Link, NavLink } from 'react-router-dom'
import { Bars3Icon } from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { useUiStore } from '../../stores/uiStore'
import { ThemeToggle } from '../theme/ThemeToggle'
import { UserMenu } from './UserMenu'

const navItems = [
  { to: '/', label: zh.nav.workbench, end: true },
  { to: '/oa/cases', label: zh.nav.cases, end: false },
  { to: '/skills', label: zh.nav.skills, end: false },
  { to: '/settings', label: zh.nav.settings, end: false },
]

export interface AppHeaderProps {
  /** SecondaryLayout 等无侧栏场景隐藏移动端菜单钮。 */
  showMobileMenuButton?: boolean
}

/**
 * §2.1 AppHeader：毛玻璃 sticky 头部，滚动自动隐藏（headerHidden 来自 uiStore，
 * 由 AppLayout 的滚动容器驱动）+ 顶部 10px 不可见悬停带唤回。
 */
export function AppHeader({ showMobileMenuButton = true }: AppHeaderProps) {
  const hidden = useUiStore((s) => s.headerHidden)
  const setHeaderHidden = useUiStore((s) => s.setHeaderHidden)
  const setMobileDrawerOpen = useUiStore((s) => s.setMobileDrawerOpen)

  return (
    <>
      {/* 顶部不可见 10px 悬停带：header 隐藏时悬停唤回 */}
      {hidden && (
        <div
          aria-hidden="true"
          className="fixed top-0 left-0 right-0 h-2.5 z-[var(--z-tooltip)]"
          onMouseEnter={() => setHeaderHidden(false)}
        />
      )}
      <motion.header
        animate={{ y: hidden ? '-100%' : 0 }}
        transition={{ duration: 0.3, ease: [0.25, 0.1, 0.25, 1] }}
        className="sticky top-0 z-[var(--z-header)] w-full bg-white/60 dark:bg-gray-900/60 backdrop-blur-md border-b border-gray-200/50 dark:border-gray-700/50 transition-colors duration-300"
      >
        <div className="w-full px-4 sm:px-10 h-[var(--app-header-h,56px)] flex items-center justify-between">
          {/* 左：移动端菜单钮 + logo + 字标 */}
          <div className="flex items-center gap-3 min-w-0">
            {showMobileMenuButton && (
              <button
                type="button"
                aria-label={zh.sidebar.openMenu}
                onClick={() => setMobileDrawerOpen(true)}
                className="md:hidden w-8 h-8 -ml-1 rounded-full flex items-center justify-center text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors"
              >
                <Bars3Icon className="w-5 h-5" strokeWidth={1.5} />
              </button>
            )}
            <Link to="/" className="flex items-center gap-2 min-w-0">
              <img src="/logo.svg?v=4" alt={zh.brand.logoAlt} className="w-6 h-6 sm:w-8 sm:h-8 shrink-0" />
              <span className="text-base sm:text-xl font-bold text-gray-900 dark:text-white truncate">
                {zh.brand.name}
              </span>
            </Link>
          </div>

          {/* 中：主导航（渐变下划线） */}
          <nav className="hidden md:flex absolute left-1/2 -translate-x-1/2 gap-12">
            {navItems.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  cn(
                    'group relative text-[15px] font-medium transition-colors',
                    isActive
                      ? 'text-gray-900 dark:text-white'
                      : 'text-gray-600 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white',
                  )
                }
              >
                {({ isActive }) => (
                  <>
                    {item.label}
                    <span
                      className={cn(
                        'absolute -bottom-1 left-0 h-0.5 bg-gradient-to-r from-blue-500 to-purple-500 transition-all duration-300',
                        isActive ? 'w-full' : 'w-0 group-hover:w-full',
                      )}
                    />
                  </>
                )}
              </NavLink>
            ))}
          </nav>

          {/* 右：工具区（主题切换 + 用户菜单） */}
          <div className="flex items-center gap-2 sm:gap-3">
            <ThemeToggle />
            <span
              aria-hidden="true"
              className="hidden sm:block w-px h-5 bg-gray-200 dark:bg-gray-700"
            />
            <UserMenu />
          </div>
        </div>
      </motion.header>
    </>
  )
}
