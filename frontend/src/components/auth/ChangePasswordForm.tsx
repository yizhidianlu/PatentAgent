import { useId, useMemo, useState, type FormEvent, type ReactNode } from 'react'
import { CheckCircleIcon, ExclamationCircleIcon } from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { checkPassword, type PasswordStrength } from '../../lib/password'
import { authErrorMessage, useChangePassword } from '../../api/auth'
import { useUiStore } from '../../stores/uiStore'
import { Button } from '../ui/Button'
import { Spinner } from '../ui/Spinner'
import { PasswordInput } from './PasswordInput'

const STRENGTH_STYLES: Record<PasswordStrength, { bar: string; text: string }> = {
  weak: { bar: 'bg-red-500', text: 'text-red-600 dark:text-red-400' },
  medium: { bar: 'bg-amber-500', text: 'text-amber-600 dark:text-amber-400' },
  strong: { bar: 'bg-emerald-500', text: 'text-emerald-600 dark:text-emerald-400' },
}

function RuleRow({ ok, label }: { ok: boolean; label: string }) {
  const Icon = ok ? CheckCircleIcon : ExclamationCircleIcon
  return (
    <li
      className={cn(
        'flex items-center gap-1.5 text-xs transition-colors',
        ok ? 'text-emerald-600 dark:text-emerald-400' : 'text-gray-400 dark:text-gray-500',
      )}
    >
      <Icon className="w-3.5 h-3.5 shrink-0" strokeWidth={1.5} />
      {label}
    </li>
  )
}

export interface ChangePasswordFormProps {
  /** 改密成功回调（页面跳转 / 关闭弹窗）。 */
  onSuccess?: () => void
  /** 提交按钮右侧的附加操作（如「返回工作台」）。 */
  extraAction?: ReactNode
  submitLabel?: string
}

/**
 * 修改密码表单（/change-password 页与设置页「修改密码」节共用）。
 * 实时展示强度校验四项：≥8 位 / 非纯数字或纯字母 / 与当前密码不同 / 两次一致。
 */
export function ChangePasswordForm({
  onSuccess,
  extraAction,
  submitLabel = zh.auth.changePassword.submit,
}: ChangePasswordFormProps) {
  const t = zh.auth.changePassword
  const oldId = useId()
  const newId = useId()
  const confirmId = useId()

  const [oldPassword, setOldPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [error, setError] = useState('')

  const pushToast = useUiStore((s) => s.pushToast)
  const changePassword = useChangePassword()

  const check = useMemo(() => checkPassword(newPassword), [newPassword])
  const differs = newPassword.length > 0 && newPassword !== oldPassword
  const match = confirmPassword.length > 0 && confirmPassword === newPassword
  const canSubmit =
    oldPassword.length > 0 && check.valid && differs && match && !changePassword.isPending

  const strengthStyle = STRENGTH_STYLES[check.strength]

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault()
    if (changePassword.isPending) return
    if (!check.valid) {
      setError(t.rules.length)
      return
    }
    if (!differs) {
      setError(t.sameAsOld)
      return
    }
    if (!match) {
      setError(t.mismatch)
      return
    }
    setError('')
    changePassword.mutate(
      { old_password: oldPassword, new_password: newPassword },
      {
        onSuccess: () => {
          pushToast('success', t.ok)
          setOldPassword('')
          setNewPassword('')
          setConfirmPassword('')
          onSuccess?.()
        },
        onError: (err) => setError(authErrorMessage(err, t.failed)),
      },
    )
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-4" noValidate>
      <div className="space-y-1.5">
        <label htmlFor={oldId} className="block text-sm font-medium text-gray-700 dark:text-gray-200">
          {t.oldPassword}
        </label>
        <PasswordInput
          id={oldId}
          value={oldPassword}
          onChange={(e) => setOldPassword(e.target.value)}
          placeholder={t.oldPasswordPlaceholder}
          autoComplete="current-password"
          autoFocus
        />
      </div>

      <div className="space-y-1.5">
        <label htmlFor={newId} className="block text-sm font-medium text-gray-700 dark:text-gray-200">
          {t.newPassword}
        </label>
        <PasswordInput
          id={newId}
          value={newPassword}
          onChange={(e) => setNewPassword(e.target.value)}
          placeholder={t.newPasswordPlaceholder}
          autoComplete="new-password"
        />
        {/* 强度条：三段，按 score 点亮 */}
        <div className="flex items-center gap-2 pt-0.5">
          <div className="flex-1 flex gap-1" aria-hidden="true">
            {[1, 2, 3].map((seg) => (
              <span
                key={seg}
                className={cn(
                  'h-1 flex-1 rounded-full transition-colors',
                  newPassword && check.score >= seg
                    ? strengthStyle.bar
                    : 'bg-gray-200 dark:bg-gray-700',
                )}
              />
            ))}
          </div>
          <span className={cn('text-[11px] font-medium shrink-0', newPassword ? strengthStyle.text : 'text-gray-400')}>
            {t.strengthLabel}
            {newPassword ? `·${t.strengths[check.strength]}` : ''}
          </span>
        </div>
      </div>

      <div className="space-y-1.5">
        <label
          htmlFor={confirmId}
          className="block text-sm font-medium text-gray-700 dark:text-gray-200"
        >
          {t.confirmPassword}
        </label>
        <PasswordInput
          id={confirmId}
          value={confirmPassword}
          onChange={(e) => setConfirmPassword(e.target.value)}
          placeholder={t.confirmPasswordPlaceholder}
          autoComplete="new-password"
        />
      </div>

      <ul className="grid grid-cols-2 gap-x-3 gap-y-1.5">
        <RuleRow ok={check.length} label={t.rules.length} />
        <RuleRow ok={check.mixed} label={t.rules.mixed} />
        <RuleRow ok={differs} label={t.rules.differs} />
        <RuleRow ok={match} label={t.rules.match} />
      </ul>

      {error && (
        <p
          role="alert"
          className="flex items-start gap-1.5 rounded-xl border border-red-200 dark:border-red-500/30 bg-red-50 dark:bg-red-500/10 px-3 py-2 text-xs text-red-600 dark:text-red-400"
        >
          <ExclamationCircleIcon className="w-4 h-4 shrink-0 mt-px" strokeWidth={1.5} />
          <span className="min-w-0">{error}</span>
        </p>
      )}

      <p className="text-[11px] text-gray-400 dark:text-gray-500">{t.logoutHint}</p>

      <div className="flex items-center gap-3">
        <Button type="submit" disabled={!canSubmit} className="flex-1">
          {changePassword.isPending ? (
            <>
              <Spinner size="sm" className="border-white/40 border-t-white" />
              {t.submitting}
            </>
          ) : (
            submitLabel
          )}
        </Button>
        {extraAction}
      </div>
    </form>
  )
}
