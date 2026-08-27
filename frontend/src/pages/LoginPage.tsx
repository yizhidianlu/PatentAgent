import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import {
  ExclamationCircleIcon,
  InformationCircleIcon,
  LockClosedIcon,
} from '@heroicons/react/24/outline'
import { zh } from '../i18n/zh'
import { authErrorMessage, displayNameOf, useLogin } from '../api/auth'
import { useAuthStore } from '../stores/authStore'
import { useUiStore } from '../stores/uiStore'
import { AuthShell } from '../components/auth/AuthShell'
import { PasswordInput } from '../components/auth/PasswordInput'
import { Button } from '../components/ui/Button'
import { Input } from '../components/ui/Input'
import { Spinner } from '../components/ui/Spinner'

/**
 * 后端锁定提示是一句中文（"账号已被临时锁定，请 5 分钟后再试…"），
 * 401 body 里没有结构化的 locked_until。这里从文案里抠出分钟数做本地倒计时——
 * 抠不到就退化为纯文案展示，绝不因为解析失败而吞掉错误信息。
 */
function parseLockSeconds(message: string): number {
  if (!message.includes('锁定')) return 0
  const minutes = /(\d+)\s*分钟/.exec(message)
  if (minutes) return Number(minutes[1]) * 60
  const seconds = /(\d+)\s*秒/.exec(message)
  if (seconds) return Number(seconds[1])
  return 0
}

function formatMmSs(total: number): string {
  const m = Math.floor(total / 60)
  const s = total % 60
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

/**
 * §6 登录页。品牌视觉延续 AuthShell（渐变 logo + 玻璃卡片 + 暗色适配）。
 * 无注册入口——账号由管理员统一创建，页面显式说明，避免用户到处找注册。
 */
export function LoginPage() {
  const t = zh.auth.login
  const navigate = useNavigate()
  const location = useLocation()
  const login = useLogin()
  const pushToast = useUiStore((s) => s.pushToast)
  const authStatus = useAuthStore((s) => s.status)

  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [lockRemaining, setLockRemaining] = useState(0)
  const usernameRef = useRef<HTMLInputElement>(null)

  /** 守卫跳转时带过来的原始去处（登录后回到那里）。 */
  const redirectTo = useMemo(() => {
    const state = location.state as { from?: string } | null
    const from = state?.from
    if (from && from.startsWith('/') && !from.startsWith('/login')) return from
    return '/'
  }, [location.state])

  useEffect(() => {
    usernameRef.current?.focus()
  }, [])

  // 已登录用户直接进工作台（手敲 /login 或后退键回到本页）
  useEffect(() => {
    if (authStatus === 'authenticated') navigate(redirectTo, { replace: true })
  }, [authStatus, navigate, redirectTo])

  // 锁定倒计时
  useEffect(() => {
    if (lockRemaining <= 0) return
    const timer = setInterval(() => {
      setLockRemaining((v) => {
        if (v <= 1) {
          clearInterval(timer)
          return 0
        }
        return v - 1
      })
    }, 1000)
    return () => clearInterval(timer)
  }, [lockRemaining])

  const locked = lockRemaining > 0
  const submitting = login.isPending
  const canSubmit = username.trim().length > 0 && password.length > 0 && !submitting && !locked

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault()
    if (submitting || locked) return
    if (!username.trim() || !password) {
      setError(t.missingFields)
      return
    }
    setError('')
    login.mutate(
      { username: username.trim(), password },
      {
        onSuccess: (data) => {
          pushToast('success', t.welcome(displayNameOf(data.user)))
          // must_change_password 的落点交给路由守卫判定，这里只管回到目标页
          navigate(data.user.must_change_password ? '/change-password' : redirectTo, {
            replace: true,
          })
        },
        onError: (err) => {
          const message = authErrorMessage(err, t.failed)
          setError(message)
          setLockRemaining(parseLockSeconds(message))
          setPassword('')
        },
      },
    )
  }

  return (
    <AuthShell
      title={t.title}
      subtitle={t.subtitle}
      footer={
        <div className="flex items-start gap-2 rounded-xl border border-[#492497]/15 dark:border-[#61d0e2]/20 bg-white/60 dark:bg-gray-800/50 backdrop-blur px-3.5 py-3 text-xs">
          <InformationCircleIcon
            className="w-4 h-4 shrink-0 mt-px text-[#492497] dark:text-[#61d0e2]"
            strokeWidth={1.5}
          />
          <div className="min-w-0">
            <p className="font-medium text-gray-700 dark:text-gray-200">{t.noSignupTitle}</p>
            <p className="mt-0.5 text-gray-500 dark:text-gray-400">{t.noSignupDesc}</p>
          </div>
        </div>
      }
    >
      <form onSubmit={handleSubmit} className="space-y-4" noValidate>
        <div className="space-y-1.5">
          <label
            htmlFor="login-username"
            className="block text-sm font-medium text-gray-700 dark:text-gray-200"
          >
            {t.username}
          </label>
          <Input
            id="login-username"
            ref={usernameRef}
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder={t.usernamePlaceholder}
            autoComplete="username"
            autoCapitalize="none"
            spellCheck={false}
            disabled={submitting}
          />
        </div>

        <div className="space-y-1.5">
          <label
            htmlFor="login-password"
            className="block text-sm font-medium text-gray-700 dark:text-gray-200"
          >
            {t.password}
          </label>
          <PasswordInput
            id="login-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder={t.passwordPlaceholder}
            autoComplete="current-password"
            disabled={submitting}
          />
        </div>

        {error && (
          <p
            role="alert"
            className="flex items-start gap-1.5 rounded-xl border border-red-200 dark:border-red-500/30 bg-red-50 dark:bg-red-500/10 px-3 py-2 text-xs text-red-600 dark:text-red-400"
          >
            <ExclamationCircleIcon className="w-4 h-4 shrink-0 mt-px" strokeWidth={1.5} />
            <span className="min-w-0">{error}</span>
          </p>
        )}

        {locked && (
          <p className="flex items-center gap-1.5 rounded-xl border border-amber-200 dark:border-amber-500/30 bg-amber-50 dark:bg-amber-500/10 px-3 py-2 text-xs font-medium text-amber-700 dark:text-amber-400">
            <LockClosedIcon className="w-4 h-4 shrink-0" strokeWidth={1.5} />
            <span className="min-w-0 tabular-nums">{t.lockedCountdown(formatMmSs(lockRemaining))}</span>
          </p>
        )}

        <Button type="submit" variant="gradient" disabled={!canSubmit} className="w-full !rounded-xl">
          {submitting ? (
            <>
              <Spinner size="sm" className="border-white/40 border-t-white" />
              {t.submitting}
            </>
          ) : (
            t.submit
          )}
        </Button>
      </form>
    </AuthShell>
  )
}
