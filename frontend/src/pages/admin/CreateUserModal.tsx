import { useState, type FormEvent } from 'react'
import { ExclamationCircleIcon } from '@heroicons/react/24/outline'
import { zh } from '../../i18n/zh'
import { ApiError } from '../../lib/api'
import { checkPassword } from '../../lib/password'
import { EMPTY_QUOTA, type Quota, type Role } from '../../api/auth'
import { useCreateUser, type PasswordIssued } from '../../api/admin'
import { Button } from '../../components/ui/Button'
import { Input } from '../../components/ui/Input'
import { Modal } from '../../components/ui/Modal'
import { Select } from '../../components/ui/Select'
import { Spinner } from '../../components/ui/Spinner'
import { PasswordInput } from '../../components/auth/PasswordInput'

/** 与 backend/app/models/auth.py USERNAME_PATTERN 保持一致。 */
const USERNAME_RE = /^[A-Za-z][A-Za-z0-9_.-]{2,31}$/

const QUOTA_FIELDS: (keyof Quota)[] = [
  'daily_llm_calls',
  'monthly_tokens',
  'storage_mb',
  'max_cases',
]

export interface CreateUserModalProps {
  open: boolean
  onClose: () => void
  /** 建号成功：把一次性密码交给上层弹 OneTimePasswordModal。 */
  onCreated: (result: PasswordIssued) => void
}

/** §6 新建用户 Modal：用户名 / 显示名 / 角色 / 可选自定义密码 / 配额四项。 */
export function CreateUserModal({ open, onClose, onCreated }: CreateUserModalProps) {
  const t = zh.admin.users.createModal
  const createUser = useCreateUser()

  const [username, setUsername] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [role, setRole] = useState<Role>('user')
  const [password, setPassword] = useState('')
  const [quota, setQuota] = useState<Quota>(EMPTY_QUOTA)
  const [error, setError] = useState('')

  const reset = () => {
    setUsername('')
    setDisplayName('')
    setRole('user')
    setPassword('')
    setQuota(EMPTY_QUOTA)
    setError('')
  }

  const handleClose = () => {
    if (createUser.isPending) return
    reset()
    onClose()
  }

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault()
    if (createUser.isPending) return
    const name = username.trim().toLowerCase()
    if (!name) {
      setError(t.usernameRequired)
      return
    }
    if (!USERNAME_RE.test(name)) {
      setError(t.usernameInvalid)
      return
    }
    if (password && !checkPassword(password).valid) {
      setError(t.passwordTooShort)
      return
    }
    setError('')
    createUser.mutate(
      {
        username: name,
        display_name: displayName.trim(),
        role,
        ...(password ? { password } : {}),
        quota,
      },
      {
        onSuccess: (result) => {
          reset()
          onCreated(result)
        },
        onError: (err) => {
          if (err instanceof ApiError && err.status === 409) {
            setError(err.message || t.conflict)
            return
          }
          setError(err instanceof Error && err.message ? err.message : t.failed)
        },
      },
    )
  }

  return (
    <Modal open={open} onClose={handleClose} title={t.title} panelClassName="max-w-xl">
      <form onSubmit={handleSubmit} className="space-y-4" noValidate>
        <p className="text-xs text-gray-500 dark:text-gray-400">{t.desc}</p>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div className="space-y-1.5">
            <label
              htmlFor="admin-new-username"
              className="block text-sm font-medium text-gray-700 dark:text-gray-200"
            >
              {t.username}
            </label>
            <Input
              id="admin-new-username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder={t.usernamePlaceholder}
              autoComplete="off"
              autoCapitalize="none"
              spellCheck={false}
            />
          </div>
          <div className="space-y-1.5">
            <label
              htmlFor="admin-new-display"
              className="block text-sm font-medium text-gray-700 dark:text-gray-200"
            >
              {t.displayName}
            </label>
            <Input
              id="admin-new-display"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder={t.displayNamePlaceholder}
              autoComplete="off"
            />
          </div>
        </div>
        <p className="-mt-2 text-xs text-gray-500 dark:text-gray-400">{t.usernameHint}</p>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div className="space-y-1.5">
            <label
              htmlFor="admin-new-role"
              className="block text-sm font-medium text-gray-700 dark:text-gray-200"
            >
              {t.role}
            </label>
            <Select
              id="admin-new-role"
              value={role}
              onChange={(e) => setRole(e.target.value as Role)}
            >
              <option value="user">{zh.auth.roles.user}</option>
              <option value="admin">{zh.auth.roles.admin}</option>
            </Select>
          </div>
          <div className="space-y-1.5">
            <label
              htmlFor="admin-new-password"
              className="block text-sm font-medium text-gray-700 dark:text-gray-200"
            >
              {t.password}
            </label>
            <PasswordInput
              id="admin-new-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder={t.passwordPlaceholder}
              autoComplete="new-password"
            />
          </div>
        </div>
        <p className="-mt-2 text-xs text-gray-500 dark:text-gray-400">{t.passwordHint}</p>

        <fieldset className="rounded-xl border border-gray-200 dark:border-gray-700 p-3.5">
          <legend className="px-1 text-xs font-medium text-gray-500 dark:text-gray-400">
            {t.quotaTitle}
          </legend>
          <div className="grid grid-cols-2 gap-3">
            {QUOTA_FIELDS.map((key) => (
              <div key={key} className="space-y-1">
                <label
                  htmlFor={`admin-quota-${key}`}
                  className="block text-xs text-gray-600 dark:text-gray-300"
                >
                  {t.quota[key]}
                </label>
                <Input
                  id={`admin-quota-${key}`}
                  type="number"
                  min={0}
                  /* 0 渲染为空串并用 placeholder 提示：否则受控值恒为 0，
                     用户既删不掉它，输入 100 只会变成 0100（0 表示不限，空亦然）。 */
                  value={quota[key] === 0 ? '' : String(quota[key])}
                  placeholder="0"
                  onChange={(e) => {
                    const raw = e.target.value
                    setQuota((q) => ({
                      ...q,
                      [key]: raw === '' ? 0 : Math.max(0, Number(raw) || 0),
                    }))
                  }}
                  className="h-9"
                />
              </div>
            ))}
          </div>
        </fieldset>

        {error && (
          <p
            role="alert"
            className="flex items-start gap-1.5 rounded-xl border border-red-200 dark:border-red-500/30 bg-red-50 dark:bg-red-500/10 px-3 py-2 text-xs text-red-600 dark:text-red-400"
          >
            <ExclamationCircleIcon className="w-4 h-4 shrink-0 mt-px" strokeWidth={1.5} />
            <span className="min-w-0">{error}</span>
          </p>
        )}

        <div className="flex justify-end gap-2 pt-1">
          <Button variant="ghost" onClick={handleClose} disabled={createUser.isPending}>
            {zh.common.cancel}
          </Button>
          <Button type="submit" disabled={createUser.isPending}>
            {createUser.isPending ? (
              <>
                <Spinner size="sm" className="border-white/40 border-t-white" />
                {t.submitting}
              </>
            ) : (
              t.submit
            )}
          </Button>
        </div>
      </form>
    </Modal>
  )
}
