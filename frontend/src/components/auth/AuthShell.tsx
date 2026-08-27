import type { ReactNode } from 'react'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { ThemeToggle } from '../theme/ThemeToggle'

export interface AuthShellProps {
  title: string
  subtitle?: string
  children: ReactNode
  /** 卡片下方的补充说明（登录页的「账号由管理员创建」提示条等）。 */
  footer?: ReactNode
  className?: string
}

/**
 * 登录 / 强制改密两页共用的品牌外壳：
 * 渐变底 + 三色光斑（#61d0e2 → #492497 → #d13870）+ 玻璃卡片，暗色自动适配。
 * 右上角保留主题切换 —— 这两页在路由守卫之外，进不去设置页也得能换深浅色。
 */
export function AuthShell({ title, subtitle, children, footer, className }: AuthShellProps) {
  return (
    <div className="relative min-h-screen w-full overflow-hidden bg-gradient-to-br from-slate-50 via-blue-50/40 to-indigo-50/60 dark:from-gray-900 dark:via-gray-800/40 dark:to-slate-900/60 flex flex-col items-center justify-center px-4 py-10">
      {/* 品牌三色光斑（纯装饰） */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute -top-40 -left-32 w-[28rem] h-[28rem] rounded-full bg-[#61d0e2]/25 dark:bg-[#61d0e2]/10 blur-3xl"
      />
      <div
        aria-hidden="true"
        className="pointer-events-none absolute top-1/3 -right-40 w-[30rem] h-[30rem] rounded-full bg-[#492497]/20 dark:bg-[#492497]/20 blur-3xl"
      />
      <div
        aria-hidden="true"
        className="pointer-events-none absolute -bottom-40 left-1/3 w-[26rem] h-[26rem] rounded-full bg-[#d13870]/15 dark:bg-[#d13870]/10 blur-3xl"
      />

      <div className="absolute top-4 right-4 z-10">
        <ThemeToggle />
      </div>

      <main className={cn('relative w-full max-w-md', className)}>
        <header className="mb-6 flex flex-col items-center text-center">
          <img src="/logo.svg?v=4" alt={zh.brand.logoAlt} className="w-12 h-12 mb-3" />
          <h1 className="text-2xl font-bold bg-gradient-to-r from-[#61d0e2] via-[#492497] to-[#d13870] bg-clip-text text-transparent">
            {zh.brand.name}
          </h1>
          <p className="mt-2 text-base font-semibold text-gray-900 dark:text-gray-100">{title}</p>
          {subtitle && (
            <p className="mt-1 text-xs text-gray-500 dark:text-gray-400 max-w-xs">{subtitle}</p>
          )}
        </header>

        <div className="glass-effect rounded-2xl border border-white/60 dark:border-white/10 shadow-xl shadow-indigo-500/5 p-6 sm:p-7">
          {children}
        </div>

        {footer && <div className="mt-5">{footer}</div>}
      </main>
    </div>
  )
}
