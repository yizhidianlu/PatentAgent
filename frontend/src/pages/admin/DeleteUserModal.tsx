import { useState, type FormEvent } from 'react'
import { ExclamationTriangleIcon } from '@heroicons/react/24/outline'
import { zh } from '../../i18n/zh'
import { displayNameOf, type UserOut } from '../../api/auth'
import { useDeleteUser } from '../../api/admin'
import { useUiStore } from '../../stores/uiStore'
import { Button } from '../../components/ui/Button'
import { Input } from '../../components/ui/Input'
import { Modal } from '../../components/ui/Modal'
import { Spinner } from '../../components/ui/Spinner'
import { ToggleSwitch } from '../../components/ui/ToggleSwitch'

export interface DeleteUserModalProps {
  user: UserOut | null
  onClose: () => void
}

/**
 * 删除用户二次确认：必须**手打用户名**才能提交。
 * 删号会连带其全部案件、上传件与交付物，误点一次无法恢复，
 * 单纯的「确定/取消」在这种破坏力面前不够。
 */
export function DeleteUserModal({ user, onClose }: DeleteUserModalProps) {
  const t = zh.admin.users.deleteModal
  const [typed, setTyped] = useState('')
  // 默认关：删账号是常见操作，删磁盘文件不可撤销，两者不该绑在一起默认发生
  const [purgeFiles, setPurgeFiles] = useState(false)
  const [error, setError] = useState('')
  const deleteUser = useDeleteUser()
  const pushToast = useUiStore((s) => s.pushToast)

  const open = Boolean(user)
  const matched = Boolean(user) && typed.trim().toLowerCase() === user?.username.toLowerCase()

  const handleClose = () => {
    if (deleteUser.isPending) return
    setTyped('')
    setError('')
    setPurgeFiles(true)
    onClose()
  }

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault()
    if (!user || deleteUser.isPending) return
    if (!matched) {
      setError(t.mismatch)
      return
    }
    setError('')
    deleteUser.mutate(
      { id: user.id, purgeFiles },
      {
        onSuccess: (result) => {
          pushToast('success', result?.message || t.ok)
          setTyped('')
          onClose()
        },
        onError: (err) => setError(err instanceof Error && err.message ? err.message : t.failed),
      },
    )
  }

  return (
    <Modal open={open} onClose={handleClose} title={t.title}>
      <form onSubmit={handleSubmit} className="space-y-4" noValidate>
        <div className="flex items-start gap-2 rounded-xl border border-red-200 dark:border-red-500/30 bg-red-50 dark:bg-red-500/10 px-3.5 py-3 text-xs text-red-600 dark:text-red-400">
          <ExclamationTriangleIcon className="w-4 h-4 shrink-0 mt-px" strokeWidth={1.5} />
          <span className="min-w-0">{t.desc(user ? displayNameOf(user) : '')}</span>
        </div>

        <div className="space-y-1.5">
          <label
            htmlFor="admin-delete-confirm"
            className="block text-sm font-medium text-gray-700 dark:text-gray-200"
          >
            {t.confirmLabel(user?.username ?? '')}
          </label>
          <Input
            id="admin-delete-confirm"
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            placeholder={t.placeholder}
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
          />
        </div>

        <div className="flex items-center justify-between gap-4">
          <p className="text-sm text-gray-700 dark:text-gray-200">{t.purgeFiles}</p>
          <ToggleSwitch checked={purgeFiles} onChange={setPurgeFiles} label={t.purgeFiles} />
        </div>

        {error && (
          <p role="alert" className="text-xs text-red-600 dark:text-red-400">
            {error}
          </p>
        )}

        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={handleClose} disabled={deleteUser.isPending}>
            {zh.common.cancel}
          </Button>
          <Button type="submit" variant="danger" disabled={!matched || deleteUser.isPending}>
            {deleteUser.isPending ? (
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
