import { useId, useState, type ReactNode } from 'react'
import {
  CheckCircleIcon,
  ExclamationTriangleIcon,
  EyeIcon,
  EyeSlashIcon,
  XCircleIcon,
} from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { Button } from '../../components/ui/Button'
import { Card } from '../../components/ui/Card'
import { Input } from '../../components/ui/Input'
import { Skeleton } from '../../components/ui/Skeleton'
import { ToggleSwitch } from '../../components/ui/ToggleSwitch'

/** 设置页四节共用的表单原语（§3.7）。 */

export interface SettingsCardProps {
  title: string
  description?: string
  children: ReactNode
  /** 卡片底部操作区（测试 / 保存）。 */
  footer?: ReactNode
}

/** 每节一张 Card p-6：标题 + 描述 + 表单 + 底部操作条。 */
export function SettingsCard({ title, description, children, footer }: SettingsCardProps) {
  return (
    <Card className="p-6">
      <header className="mb-5">
        <h2 className="text-base font-semibold text-gray-900 dark:text-gray-100">{title}</h2>
        {description && (
          <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">{description}</p>
        )}
      </header>
      <div className="space-y-5">{children}</div>
      {footer && (
        <footer className="mt-6 pt-4 border-t border-gray-100 dark:border-gray-700/60 flex flex-wrap items-center justify-end gap-3">
          {footer}
        </footer>
      )}
    </Card>
  )
}

export interface FieldProps {
  label: string
  hint?: string
  /** 标签行右端的附注（如 token 数的易读写法 `1M`）。留空则整块不渲染。 */
  badge?: string
  children: (id: string) => ReactNode
  className?: string
}

/** 标签 + 控件 + 提示行。children 收到 id，供 label htmlFor 关联。 */
export function Field({ label, hint, badge, children, className }: FieldProps) {
  const id = useId()
  return (
    <div className={cn('space-y-1.5', className)}>
      <div className="flex items-baseline justify-between gap-2">
        <label htmlFor={id} className="block text-sm font-medium text-gray-700 dark:text-gray-200">
          {label}
        </label>
        {badge && (
          // 做成浅底小徽章而不是一行灰字：它是对下方数字的换算，不是又一个可编辑项，
          // 视觉上要能一眼与标签区分开。等宽 + tabular-nums 让不同字段的简写竖向对齐。
          <span
            className="shrink-0 rounded px-1.5 py-0.5 font-mono text-[11px] leading-none
                       tabular-nums bg-gray-100 text-gray-500
                       dark:bg-gray-800 dark:text-gray-400"
          >
            {badge}
          </span>
        )}
      </div>
      {children(id)}
      {hint && <p className="text-xs text-gray-500 dark:text-gray-400">{hint}</p>}
    </div>
  )
}

export interface ToggleRowProps {
  title: string
  description?: string
  checked: boolean
  onChange: (checked: boolean) => void
  disabled?: boolean
}

/** `flex items-center justify-between`：左标题+描述，右开关。 */
export function ToggleRow({ title, description, checked, onChange, disabled }: ToggleRowProps) {
  return (
    <div className="flex items-center justify-between gap-4">
      <div className="min-w-0">
        <p className="text-sm font-medium text-gray-700 dark:text-gray-200">{title}</p>
        {description && (
          <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">{description}</p>
        )}
      </div>
      <ToggleSwitch checked={checked} onChange={onChange} disabled={disabled} label={title} />
    </div>
  )
}

export interface ApiKeyInputProps {
  id?: string
  value: string
  onChange: (value: string) => void
  placeholder?: string
}

/** API Key 输入：password + EyeIcon 明暗切换（后端只回掩码，留空 = 不修改）。 */
export function ApiKeyInput({ id, value, onChange, placeholder }: ApiKeyInputProps) {
  const [visible, setVisible] = useState(false)
  const Icon = visible ? EyeSlashIcon : EyeIcon
  return (
    <div className="relative">
      <Input
        id={id}
        type={visible ? 'text' : 'password'}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        autoComplete="off"
        spellCheck={false}
        className="pr-11 font-mono"
      />
      <button
        type="button"
        onClick={() => setVisible((v) => !v)}
        aria-label={visible ? zh.settings.common.hideKey : zh.settings.common.showKey}
        className="absolute right-2 top-1/2 -translate-y-1/2 w-7 h-7 rounded-lg flex items-center justify-center text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
      >
        <Icon className="w-4 h-4" strokeWidth={1.5} />
      </button>
    </div>
  )
}

export type TestStatus =
  | { kind: 'idle' }
  | { kind: 'ok'; text: string; detail?: string | null }
  | { kind: 'error'; text: string; detail?: string | null }

/** 连接测试结果：成功 emerald 徽章 / 失败 red 徽章 + 错误详情 text-xs。 */
export function TestResult({ status }: { status: TestStatus }) {
  if (status.kind === 'idle') return null
  const ok = status.kind === 'ok'
  const Icon = ok ? CheckCircleIcon : XCircleIcon
  return (
    <div className="min-w-0 flex-1 space-y-1">
      <span
        className={cn(
          'inline-flex items-center gap-1.5 rounded-lg px-2 py-1 text-xs font-medium',
          ok
            ? 'bg-emerald-50 dark:bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
            : 'bg-red-50 dark:bg-red-500/10 text-red-600 dark:text-red-400',
        )}
      >
        <Icon className="w-3.5 h-3.5" strokeWidth={1.5} />
        {status.text}
      </span>
      {status.kind === 'ok' && status.detail && (
        <p className="text-xs text-gray-500 dark:text-gray-400 break-words">{status.detail}</p>
      )}
      {status.kind === 'error' && status.detail && (
        <p className="text-xs text-red-500 dark:text-red-400 break-all line-clamp-3">
          {status.detail}
        </p>
      )}
    </div>
  )
}

/** amber 提示条（维度变更 / 实测维度不一致等）。 */
export function WarningBanner({ children }: { children: ReactNode }) {
  return (
    <div className="flex items-start gap-2 rounded-xl border border-amber-200 dark:border-amber-500/30 bg-amber-50 dark:bg-amber-500/10 px-3.5 py-2.5 text-xs text-amber-700 dark:text-amber-400">
      <ExclamationTriangleIcon className="w-4 h-4 shrink-0 mt-px" strokeWidth={1.5} />
      <span className="min-w-0">{children}</span>
    </div>
  )
}

/** 设置加载骨架（每节共用）。 */
export function SettingsSkeleton() {
  return (
    <Card className="p-6 space-y-4" aria-label={zh.settings.common.loading}>
      <Skeleton className="h-5 w-32 rounded-lg" />
      <Skeleton className="h-3 w-64 rounded" />
      <Skeleton className="h-10 w-full rounded-xl" />
      <Skeleton className="h-10 w-full rounded-xl" />
      <Skeleton className="h-10 w-full rounded-xl" />
    </Card>
  )
}

/**
 * 设置读取失败态。
 *
 * 必须与「未配置」明确区分：读取失败时若照常渲染一张空表单，用户会误以为配置丢了，
 * 一旦按下保存就会把空值真的写进去。因此失败时只给提示 + 重新加载，不渲染表单。
 */
export function SettingsLoadError({ onRetry, detail }: { onRetry: () => void; detail?: string }) {
  return (
    <Card className="p-6 flex flex-wrap items-center justify-between gap-4">
      <div className="flex items-start gap-2 min-w-0">
        <ExclamationTriangleIcon className="w-5 h-5 shrink-0 text-red-500" strokeWidth={1.5} />
        <div className="min-w-0">
          <p className="text-sm text-gray-700 dark:text-gray-200">{zh.settings.common.loadFailed}</p>
          {detail && (
            <p className="mt-0.5 text-xs text-red-500 dark:text-red-400 break-all line-clamp-2">
              {detail}
            </p>
          )}
        </div>
      </div>
      <Button variant="secondary" onClick={onRetry}>
        {zh.settings.common.reload}
      </Button>
    </Card>
  )
}

/** 可用 / 不可用徽章（环境自检）。 */
export function StatusBadge({ ok, okText, failText }: { ok: boolean; okText?: string; failText?: string }) {
  return (
    <span
      className={cn(
        'inline-flex items-center shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold',
        ok
          ? 'bg-emerald-50 dark:bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
          : 'bg-red-50 dark:bg-red-500/10 text-red-600 dark:text-red-400',
      )}
    >
      {ok ? (okText ?? zh.settings.appearance.available) : (failText ?? zh.settings.appearance.unavailable)}
    </span>
  )
}
