import { useState } from 'react'
import {
  CheckIcon,
  ClipboardDocumentIcon,
  ExclamationTriangleIcon,
} from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { displayNameOf, type UserOut } from '../../api/auth'
import { useUiStore } from '../../stores/uiStore'
import { Button } from '../../components/ui/Button'
import { Modal } from '../../components/ui/Modal'
import { RoleBadge } from '../../components/layout/UserMenu'

export interface OneTimePasswordModalProps {
  open: boolean
  /** 建号 = created，重置 = reset（只影响标题）。 */
  kind: 'created' | 'reset'
  user: UserOut | null
  password: string
  note?: string
  onClose: () => void
}

/** 尽力复制到剪贴板；不可用时退回 execCommand（http 明文页 clipboard API 不可用）。 */
async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    try {
      const ta = document.createElement('textarea')
      ta.value = text
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.select()
      const ok = document.execCommand('copy')
      ta.remove()
      return ok
    } catch {
      return false
    }
  }
}

/**
 * 一次性密码展示 Modal（§6）。
 *
 * 后端只在建号 / 重置的那一次响应里返回明文密码，之后任何接口都取不到。
 * 因此这里：大字号等宽展示 + 复制按钮 + 醒目警告，且**关闭前二次确认**——
 * 手滑点掉窗口就等于把这个账号的密码弄丢了，只能再重置一次。
 */
export function OneTimePasswordModal({
  open,
  kind,
  user,
  password,
  note,
  onClose,
}: OneTimePasswordModalProps) {
  const t = zh.admin.users.passwordModal
  const [copied, setCopied] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const pushToast = useUiStore((s) => s.pushToast)

  const handleCopy = () => {
    void copyText(password).then((ok) => {
      if (ok) {
        setCopied(true)
        pushToast('success', t.copied)
        window.setTimeout(() => setCopied(false), 2000)
      } else {
        pushToast('error', t.copyFailed)
      }
    })
  }

  const requestClose = () => setConfirming(true)

  const finalClose = () => {
    setConfirming(false)
    setCopied(false)
    onClose()
  }

  return (
    <Modal
      open={open}
      onClose={requestClose}
      title={kind === 'created' ? t.createdTitle : t.resetTitle}
      panelClassName="max-w-lg"
    >
      <div className="space-y-4">
        {user && (
          <div className="flex items-center gap-2 text-sm">
            <span className="text-gray-500 dark:text-gray-400">{t.userLabel}</span>
            <span className="font-medium text-gray-900 dark:text-gray-100">
              {displayNameOf(user)}
            </span>
            <span className="text-xs text-gray-400 dark:text-gray-500">@{user.username}</span>
            <RoleBadge role={user.role} />
          </div>
        )}

        {/* 醒目警告 */}
        <div className="flex items-start gap-2 rounded-xl border-2 border-amber-300 dark:border-amber-500/40 bg-amber-50 dark:bg-amber-500/10 px-3.5 py-3">
          <ExclamationTriangleIcon
            className="w-5 h-5 shrink-0 text-amber-500"
            strokeWidth={1.8}
          />
          <div className="min-w-0">
            <p className="text-sm font-bold text-amber-700 dark:text-amber-400">{t.warning}</p>
            <p className="mt-1 text-xs text-amber-700/80 dark:text-amber-400/80">
              {note || t.warningDesc}
            </p>
          </div>
        </div>

        {/* 密码：大字号等宽，允许选中 */}
        <div className="space-y-1.5">
          <p className="text-xs font-medium text-gray-500 dark:text-gray-400">{t.passwordLabel}</p>
          <div className="flex items-stretch gap-2">
            <code className="flex-1 min-w-0 select-all break-all rounded-xl border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900/50 px-4 py-3 font-mono text-lg sm:text-xl font-semibold tracking-wide text-gray-900 dark:text-gray-100">
              {password}
            </code>
            <Button
              variant="secondary"
              onClick={handleCopy}
              className={cn('shrink-0 !px-3', copied && 'text-emerald-600 dark:text-emerald-400')}
              aria-label={t.copy}
            >
              {copied ? (
                <CheckIcon className="w-5 h-5" strokeWidth={2} />
              ) : (
                <ClipboardDocumentIcon className="w-5 h-5" strokeWidth={1.5} />
              )}
              <span className="hidden sm:inline">{copied ? t.copied : t.copy}</span>
            </Button>
          </div>
        </div>

        {/* 关闭前二次确认 */}
        {confirming ? (
          <div className="rounded-xl border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900/40 p-3.5 space-y-3">
            <div>
              <p className="text-sm font-medium text-gray-900 dark:text-gray-100">
                {t.confirmTitle}
              </p>
              <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">{t.confirmDesc}</p>
            </div>
            <div className="flex justify-end gap-2">
              <Button variant="ghost" size="sm" onClick={() => setConfirming(false)}>
                {t.confirmNo}
              </Button>
              <Button variant="danger" size="sm" onClick={finalClose}>
                {t.confirmYes}
              </Button>
            </div>
          </div>
        ) : (
          <div className="flex justify-end">
            <Button variant="secondary" onClick={requestClose}>
              {t.close}
            </Button>
          </div>
        )}
      </div>
    </Modal>
  )
}
