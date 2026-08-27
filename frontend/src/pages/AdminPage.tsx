import type { ComponentType, SVGProps } from 'react'
import { NavLink, Outlet } from 'react-router-dom'
import {
  ChartBarIcon,
  ClipboardDocumentListIcon,
  UsersIcon,
} from '@heroicons/react/24/outline'
import { cn } from '../lib/cn'
import { zh } from '../i18n/zh'

const NAV: { to: string; label: string; Icon: ComponentType<SVGProps<SVGSVGElement>> }[] = [
  { to: '/admin/users', label: zh.admin.nav.users, Icon: UsersIcon },
  { to: '/admin/audit', label: zh.admin.nav.audit, Icon: ClipboardDocumentListIcon },
  { to: '/admin/stats', label: zh.admin.nav.stats, Icon: ChartBarIcon },
]

/**
 * §6 管理后台外壳（SecondaryLayout 内）：左侧 w-64 导航 aside
 * （移动端横向 pill tabs）+ 右侧内容区，与设置页同构。
 */
export function AdminPage() {
  return (
    <div className="flex-1 min-h-0 flex">
      <aside className="hidden lg:block w-64 shrink-0 bg-white dark:bg-gray-800 border-r border-gray-200 dark:border-gray-700 py-4 overflow-y-auto">
        <h1 className="px-6 pb-2 text-[11px] font-semibold uppercase tracking-wide text-gray-400 dark:text-gray-500">
          {zh.admin.title}
        </h1>
        <nav aria-label={zh.admin.title}>
          {NAV.map(({ to, label, Icon }) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) =>
                cn(
                  'w-[calc(100%-1rem)] mx-2 px-4 py-2.5 rounded-lg text-sm flex items-center gap-3 transition-colors',
                  'hover:bg-gray-50 dark:hover:bg-gray-700/50',
                  isActive
                    ? 'bg-indigo-50 dark:bg-indigo-500/10 text-[#6366f1] font-medium'
                    : 'text-gray-600 dark:text-gray-300',
                )
              }
            >
              <Icon className="w-5 h-5 shrink-0" strokeWidth={1.5} />
              {label}
            </NavLink>
          ))}
        </nav>
      </aside>

      <div className="flex-1 min-w-0 overflow-y-auto">
        {/* 移动端：横向 pill tabs */}
        <div className="lg:hidden sticky top-0 z-10 bg-white/80 dark:bg-gray-900/80 backdrop-blur-md border-b border-gray-200 dark:border-gray-700 px-4 py-3 space-y-2">
          <h1 className="text-base font-bold tracking-tight text-gray-900 dark:text-gray-100">
            {zh.admin.title}
          </h1>
          <nav aria-label={zh.admin.title} className="flex items-center gap-2 overflow-x-auto scrollbar-thin">
            {NAV.map(({ to, label, Icon }) => (
              <NavLink
                key={to}
                to={to}
                className={({ isActive }) =>
                  cn(
                    'shrink-0 inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-xs font-medium transition-colors',
                    isActive
                      ? 'bg-indigo-50 dark:bg-indigo-500/10 text-[#6366f1]'
                      : 'bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400',
                  )
                }
              >
                <Icon className="w-3.5 h-3.5" strokeWidth={1.5} />
                {label}
              </NavLink>
            ))}
          </nav>
        </div>

        <div className="px-4 sm:px-8 py-6">
          <Outlet />
        </div>
      </div>
    </div>
  )
}
