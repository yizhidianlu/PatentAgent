import { useNavigate } from 'react-router-dom'
import { ExclamationTriangleIcon } from '@heroicons/react/24/outline'
import { zh } from '../i18n/zh'
import { useCurrentUser } from '../stores/authStore'
import { AuthShell } from '../components/auth/AuthShell'
import { ChangePasswordForm } from '../components/auth/ChangePasswordForm'
import { Button } from '../components/ui/Button'

/**
 * §6 强制改密页。
 *
 * `must_change_password=true` 时路由守卫会把**任何**路由都重定向到此页
 * （见 routes/guards.tsx）；用户也可以从头部用户菜单主动进来改密，
 * 此时页面文案与「返回工作台」按钮都会切成非强制版本。
 */
export function ChangePasswordPage() {
  const t = zh.auth.changePassword
  const navigate = useNavigate()
  const user = useCurrentUser()
  const forced = Boolean(user?.must_change_password)

  return (
    <AuthShell
      title={forced ? t.forcedTitle : t.title}
      subtitle={forced ? t.forcedSubtitle : t.subtitle}
      footer={
        forced ? (
          <div className="flex items-start gap-2 rounded-xl border border-amber-200 dark:border-amber-500/30 bg-amber-50/80 dark:bg-amber-500/10 px-3.5 py-3 text-xs text-amber-700 dark:text-amber-400">
            <ExclamationTriangleIcon className="w-4 h-4 shrink-0 mt-px" strokeWidth={1.5} />
            <span className="min-w-0">{t.forcedFooter}</span>
          </div>
        ) : null
      }
    >
      <ChangePasswordForm
        onSuccess={() => navigate('/', { replace: true })}
        extraAction={
          forced ? undefined : (
            <Button variant="secondary" onClick={() => navigate('/')}>
              {t.backToWorkbench}
            </Button>
          )
        }
      />
    </AuthShell>
  )
}
