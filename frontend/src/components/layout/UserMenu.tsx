import { useNavigate } from 'react-router-dom'
import {
  ArrowRightStartOnRectangleIcon,
  KeyIcon,
  ShieldCheckIcon,
} from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { displayNameOf, useLogout } from '../../api/auth'
import { useCurrentUser } from '../../stores/authStore'
import { useUiStore } from '../../stores/uiStore'
import { Dropdown, DropdownItem } from '../ui/Dropdown'

/** 头像文字：显示名/用户名首字（中文取首字，英文取首字母大写）。 */
function initialOf(name: string): string {
  return name ? name.slice(0, 1).toUpperCase() : '?'
}

/** 角色徽章：管理员用品牌渐变，普通用户用中性灰。 */
export function RoleBadge({ role, className }: { role: string; className?: string }) {
  const isAdmin = role === 'admin'
  return (
    <span
      className={cn(
        'inline-flex items-center shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold',
        isAdmin
          ? 'bg-gradient-to-r from-[#61d0e2] via-[#492497] to-[#d13870] text-white shadow-sm'
          : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300',
        className,
      )}
    >
      {zh.auth.roles[role] ?? role}
    </span>
  )
}

/**
 * §6 头部用户菜单：显示名 + 角色徽章；下拉含 修改密码 / 退出登录，
 * 管理员多一个「管理后台」入口。
 */
export function UserMenu() {
  const user = useCurrentUser()
  const navigate = useNavigate()
  const logout = useLogout()
  const pushToast = useUiStore((s) => s.pushToast)

  if (!user) return null

  const name = displayNameOf(user)
  const isAdmin = user.role === 'admin'

  const handleLogout = () => {
    logout.mutate(undefined, {
      onSettled: () => {
        pushToast('info', zh.auth.userMenu.logoutOk)
        navigate('/login', { replace: true })
      },
    })
  }

  return (
    <Dropdown
      align="right"
      panelClassName="w-60"
      trigger={({ open, toggle }) => (
        <button
          type="button"
          onClick={toggle}
          aria-expanded={open}
          aria-label={zh.auth.userMenu.label}
          className={cn(
            'flex items-center gap-2 rounded-full pl-1 pr-1 sm:pr-2.5 py-1 transition-colors',
            'hover:bg-gray-100 dark:hover:bg-gray-800',
            open && 'bg-gray-100 dark:bg-gray-800',
          )}
        >
          <span
            className={cn(
              'w-7 h-7 shrink-0 rounded-full flex items-center justify-center text-xs font-semibold text-white',
              isAdmin
                ? 'bg-gradient-to-br from-[#61d0e2] via-[#492497] to-[#d13870]'
                : 'bg-gray-400 dark:bg-gray-600',
            )}
            aria-hidden="true"
          >
            {initialOf(name)}
          </span>
          <span className="hidden sm:block max-w-[8rem] truncate text-sm font-medium text-gray-700 dark:text-gray-200">
            {name}
          </span>
        </button>
      )}
    >
      {({ close }) => (
        <>
          <div className="px-3.5 pb-2 pt-1 border-b border-gray-100 dark:border-gray-700/60">
            <p className="flex items-center gap-2 min-w-0">
              <span className="flex-1 truncate text-sm font-medium text-gray-900 dark:text-gray-100">
                {name}
              </span>
              <RoleBadge role={user.role} />
            </p>
            <p className="mt-0.5 truncate text-xs text-gray-400 dark:text-gray-500">
              @{user.username}
            </p>
          </div>

          <div className="pt-1">
            {isAdmin && (
              <DropdownItem
                onClick={() => {
                  close()
                  navigate('/admin/users')
                }}
              >
                <ShieldCheckIcon className="w-4 h-4 text-gray-400 dark:text-gray-500" strokeWidth={1.5} />
                <span className="flex-1">{zh.auth.userMenu.adminConsole}</span>
              </DropdownItem>
            )}
            <DropdownItem
              onClick={() => {
                close()
                navigate('/change-password')
              }}
            >
              <KeyIcon className="w-4 h-4 text-gray-400 dark:text-gray-500" strokeWidth={1.5} />
              <span className="flex-1">{zh.auth.userMenu.changePassword}</span>
            </DropdownItem>
            <DropdownItem
              danger
              disabled={logout.isPending}
              onClick={() => {
                close()
                handleLogout()
              }}
            >
              <ArrowRightStartOnRectangleIcon className="w-4 h-4" strokeWidth={1.5} />
              <span className="flex-1">{zh.auth.userMenu.logout}</span>
            </DropdownItem>
          </div>
        </>
      )}
    </Dropdown>
  )
}
