import { useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { CheckCircleIcon, InformationCircleIcon } from '@heroicons/react/24/outline'
import { AuthShell } from '../components/auth/AuthShell'
import { Button } from '../components/ui/Button'
import { Input } from '../components/ui/Input'
import { authErrorMessage, useRegister, useRegistrationOpen } from '../api/auth'
import { zh } from '../i18n/zh'

const t = zh.auth.register

/** 与后端 USERNAME_PATTERN 保持一致：字母开头，3-32 位。 */
const USERNAME_RE = /^[A-Za-z][A-Za-z0-9_.-]{2,31}$/
const MIN_PASSWORD = 8

/**
 * 自助注册页。
 *
 * 提交成功后**停在本页显示结果**，不跳转也不自动登录——账号是 pending 状态，
 * 跳去工作台只会撞上 401，跳回登录页则会让人立刻试着登录、再收到一次拒绝。
 * 明确告诉他「在等审核」，才不会反复重试或重复注册。
 */
export function RegisterPage() {
  const navigate = useNavigate()
  const register = useRegister()
  const openQuery = useRegistrationOpen()

  const [username, setUsername] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)

  const submitting = register.isPending
  const registrationClosed = openQuery.data?.open === false

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault()
    if (submitting) return
    setError(null)

    const name = username.trim()
    if (!USERNAME_RE.test(name)) {
      setError(t.badUsername)
      return
    }
    if (password.length < MIN_PASSWORD) {
      setError(t.tooShort)
      return
    }
    if (password !== confirm) {
      setError(t.mismatch)
      return
    }

    register.mutate(
      { username: name, password, display_name: displayName.trim() },
      {
        onSuccess: (data) => setDone(data.message),
        onError: (err) => {
          setError(authErrorMessage(err, t.failed))
          setPassword('')
          setConfirm('')
        },
      },
    )
  }

  if (registrationClosed) {
    return (
      <AuthShell title={t.closedTitle} subtitle={t.closedDesc}>
        <Button type="button" className="w-full" onClick={() => navigate('/login', { replace: true })}>
          {t.backToLogin}
        </Button>
      </AuthShell>
    )
  }

  if (done) {
    return (
      <AuthShell title={t.doneTitle}>
        <div className="space-y-4">
          <div className="flex items-start gap-2.5 rounded-xl border border-emerald-200 dark:border-emerald-500/30 bg-emerald-50/70 dark:bg-emerald-500/10 px-3.5 py-3">
            <CheckCircleIcon
              className="w-5 h-5 shrink-0 mt-px text-emerald-600 dark:text-emerald-400"
              strokeWidth={1.5}
            />
            <div className="min-w-0 space-y-1">
              <p className="text-sm text-gray-800 dark:text-gray-100">{done}</p>
              <p className="text-xs text-gray-500 dark:text-gray-400">{t.doneHint}</p>
            </div>
          </div>
          <Button type="button" className="w-full" onClick={() => navigate('/login', { replace: true })}>
            {t.backToLogin}
          </Button>
        </div>
      </AuthShell>
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
          <p className="min-w-0 text-gray-500 dark:text-gray-400">{t.doneHint}</p>
        </div>
      }
    >
      <form onSubmit={handleSubmit} className="space-y-4" noValidate>
        <div className="space-y-1.5">
          <label htmlFor="reg-username" className="block text-sm font-medium text-gray-700 dark:text-gray-200">
            {t.username}
          </label>
          <Input
            id="reg-username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder={t.usernamePlaceholder}
            autoComplete="username"
            autoFocus
            disabled={submitting}
          />
          <p className="text-xs text-gray-400 dark:text-gray-500">{t.usernameHint}</p>
        </div>

        <div className="space-y-1.5">
          <label htmlFor="reg-display" className="block text-sm font-medium text-gray-700 dark:text-gray-200">
            {t.displayName}
          </label>
          <Input
            id="reg-display"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            placeholder={t.displayNamePlaceholder}
            autoComplete="nickname"
            disabled={submitting}
          />
        </div>

        <div className="space-y-1.5">
          <label htmlFor="reg-password" className="block text-sm font-medium text-gray-700 dark:text-gray-200">
            {t.password}
          </label>
          <Input
            id="reg-password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder={t.passwordPlaceholder}
            autoComplete="new-password"
            disabled={submitting}
          />
        </div>

        <div className="space-y-1.5">
          <label htmlFor="reg-confirm" className="block text-sm font-medium text-gray-700 dark:text-gray-200">
            {t.passwordConfirm}
          </label>
          <Input
            id="reg-confirm"
            type="password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            autoComplete="new-password"
            disabled={submitting}
          />
        </div>

        {error && (
          <p className="text-xs text-red-600 dark:text-red-400" role="alert">
            {error}
          </p>
        )}

        <Button type="submit" className="w-full" disabled={submitting}>
          {submitting ? t.submitting : t.submit}
        </Button>

        <p className="text-center text-xs">
          <Link to="/login" className="text-[#492497] dark:text-[#61d0e2] hover:underline">
            {t.backToLogin}
          </Link>
        </p>
      </form>
    </AuthShell>
  )
}
