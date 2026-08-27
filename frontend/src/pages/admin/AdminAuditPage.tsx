import { useState } from 'react'
import { ArrowPathIcon, ChevronRightIcon, ClipboardDocumentListIcon } from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { formatDateTime } from '../../lib/format'
import { useAdminUsers, useAuditLog, type AuditEntry } from '../../api/admin'
import { displayNameOf } from '../../api/auth'
import { Button } from '../../components/ui/Button'
import { Card } from '../../components/ui/Card'
import { EmptyState } from '../../components/ui/EmptyState'
import { Select } from '../../components/ui/Select'
import { Skeleton } from '../../components/ui/Skeleton'

const t = zh.admin.audit

/** action → 徽章配色。cross_user_read 单独 amber 突出（管理员越权读的唯一线索）。 */
const ACTION_STYLE: Record<string, string> = {
  login: 'bg-emerald-50 dark:bg-emerald-500/10 text-emerald-600 dark:text-emerald-400',
  login_failed: 'bg-red-50 dark:bg-red-500/10 text-red-600 dark:text-red-400',
  logout: 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300',
  user_create: 'bg-indigo-50 dark:bg-indigo-500/10 text-[#6366f1] dark:text-indigo-400',
  user_update: 'bg-indigo-50 dark:bg-indigo-500/10 text-[#6366f1] dark:text-indigo-400',
  user_delete: 'bg-red-50 dark:bg-red-500/10 text-red-600 dark:text-red-400',
  password_reset: 'bg-amber-50 dark:bg-amber-500/10 text-amber-600 dark:text-amber-400',
  password_change: 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300',
  cross_user_read:
    'bg-amber-100 dark:bg-amber-500/20 text-amber-700 dark:text-amber-300 ring-1 ring-amber-300 dark:ring-amber-500/40',
  settings_update: 'bg-indigo-50 dark:bg-indigo-500/10 text-[#6366f1] dark:text-indigo-400',
  case_delete: 'bg-red-50 dark:bg-red-500/10 text-red-600 dark:text-red-400',
}

const ACTION_OPTIONS = Object.keys(zh.admin.audit.actions)

function ActionBadge({ action }: { action: string }) {
  return (
    <span
      className={cn(
        'inline-flex items-center shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold whitespace-nowrap',
        ACTION_STYLE[action] ?? 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300',
      )}
    >
      {t.actions[action] ?? action}
    </span>
  )
}

function AuditRow({ entry }: { entry: AuditEntry }) {
  const [open, setOpen] = useState(false)
  const hasDetail = entry.detail && Object.keys(entry.detail).length > 0
  const crossUser = entry.action === 'cross_user_read'

  return (
    <>
      <tr
        className={cn(
          'border-t border-gray-100 dark:border-gray-700/60',
          crossUser ? 'bg-amber-50/50 dark:bg-amber-500/5' : 'hover:bg-gray-50/70 dark:hover:bg-gray-700/20',
        )}
      >
        <td className="px-4 py-2.5 text-xs text-gray-500 dark:text-gray-400 whitespace-nowrap align-top">
          {formatDateTime(entry.created_at)}
        </td>
        <td className="px-4 py-2.5 text-sm text-gray-800 dark:text-gray-100 align-top">
          {entry.actor_name || t.systemActor}
        </td>
        <td className="px-4 py-2.5 align-top">
          <ActionBadge action={entry.action} />
        </td>
        <td className="px-4 py-2.5 text-xs text-gray-500 dark:text-gray-400 align-top">
          {entry.target_type ? (
            <span className="block">
              {t.targetTypes[entry.target_type] ?? entry.target_type}
              {entry.target_id ? (
                <span className="ml-1 font-mono text-[11px] text-gray-400 break-all">
                  {entry.target_id}
                </span>
              ) : null}
            </span>
          ) : (
            '—'
          )}
          {entry.target_owner && (
            <span className="mt-0.5 block text-[11px] text-amber-600 dark:text-amber-400">
              {t.ownerLabel(entry.target_owner)}
            </span>
          )}
        </td>
        <td className="px-4 py-2.5 text-xs font-mono text-gray-400 dark:text-gray-500 align-top whitespace-nowrap">
          {entry.ip || '—'}
        </td>
        <td className="px-4 py-2.5 text-right align-top">
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
            className="inline-flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400 hover:text-[#6366f1] transition-colors"
          >
            <ChevronRightIcon
              className={cn('w-3.5 h-3.5 transition-transform', open && 'rotate-90')}
              strokeWidth={2}
            />
            {open ? t.collapse : t.expand}
          </button>
        </td>
      </tr>
      {open && (
        <tr className="border-t border-gray-100 dark:border-gray-700/60 bg-gray-50 dark:bg-gray-900/40">
          <td colSpan={6} className="px-4 py-3">
            {hasDetail ? (
              <pre className="max-h-60 overflow-auto rounded-lg bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 p-3 text-[11px] leading-relaxed font-mono text-gray-700 dark:text-gray-200 whitespace-pre-wrap break-all">
                {JSON.stringify(entry.detail, null, 2)}
              </pre>
            ) : (
              <p className="text-xs text-gray-400">{t.noDetail}</p>
            )}
          </td>
        </tr>
      )}
    </>
  )
}

export function AdminAuditPage() {
  const [action, setAction] = useState('')
  const [actorId, setActorId] = useState('')
  const [limit, setLimit] = useState(100)

  const query = useAuditLog({ action, actor_id: actorId, limit })
  // 操作者下拉直接复用用户列表（审计里的 actor_name 是快照，可能已改名）
  const usersQuery = useAdminUsers({ limit: 200 })

  const items = query.data?.items ?? []
  const total = query.data?.total ?? 0

  return (
    <div className="max-w-6xl space-y-4">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-base font-semibold text-gray-900 dark:text-gray-100">{t.title}</h2>
          <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">{t.desc}</p>
        </div>
      </header>

      <div className="flex flex-wrap items-center gap-2">
        <Select
          value={action}
          onChange={(e) => setAction(e.target.value)}
          aria-label={t.filterAction}
          className="w-44"
        >
          <option value="">{`${t.filterAction}：${t.filterAll}`}</option>
          {ACTION_OPTIONS.map((key) => (
            <option key={key} value={key}>
              {t.actions[key]}
            </option>
          ))}
        </Select>
        <Select
          value={actorId}
          onChange={(e) => setActorId(e.target.value)}
          aria-label={t.filterActor}
          className="w-44"
        >
          <option value="">{`${t.filterActor}：${t.filterAll}`}</option>
          {(usersQuery.data?.items ?? []).map((u) => (
            <option key={u.id} value={u.id}>
              {displayNameOf(u)}
            </option>
          ))}
        </Select>
        <Button
          variant="secondary"
          onClick={() => void query.refetch()}
          aria-label={zh.admin.reload}
          className="!px-3"
        >
          <ArrowPathIcon
            className={cn('w-4 h-4', query.isFetching && 'animate-spin')}
            strokeWidth={1.5}
          />
        </Button>
        <span className="ml-auto text-xs text-gray-400 dark:text-gray-500">{t.total(total)}</span>
      </div>

      <Card className="overflow-hidden">
        {query.isPending ? (
          <div className="p-4 space-y-2">
            {[0, 1, 2, 3, 4].map((i) => (
              <Skeleton key={i} shimmer className="h-9 rounded-lg" />
            ))}
          </div>
        ) : query.isError ? (
          <div className="p-8 text-center space-y-3">
            <p className="text-sm text-red-500">{zh.admin.loadFailed}</p>
            <Button variant="secondary" size="sm" onClick={() => void query.refetch()}>
              {zh.admin.reload}
            </Button>
          </div>
        ) : items.length === 0 ? (
          <EmptyState
            icon={ClipboardDocumentListIcon}
            title={t.emptyTitle}
            description={t.emptyDesc}
          />
        ) : (
          <div className="overflow-x-auto">
            {/* min-w：窄屏横向滚动，避免时间戳与 action 徽章被压成竖排 */}
            <table className="w-full min-w-[48rem] text-sm">
              <thead className="bg-gray-50 dark:bg-gray-900/40">
                <tr className="text-left text-xs text-gray-500 dark:text-gray-400 whitespace-nowrap">
                  <th className="px-4 py-2.5 font-medium">{t.columns.time}</th>
                  <th className="px-4 py-2.5 font-medium">{t.columns.actor}</th>
                  <th className="px-4 py-2.5 font-medium">{t.columns.action}</th>
                  <th className="px-4 py-2.5 font-medium">{t.columns.target}</th>
                  <th className="px-4 py-2.5 font-medium">{t.columns.ip}</th>
                  <th className="px-4 py-2.5 font-medium text-right">{t.columns.detail}</th>
                </tr>
              </thead>
              <tbody>
                {items.map((entry) => (
                  <AuditRow key={entry.id} entry={entry} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {items.length > 0 && total > items.length && (
        <div className="flex justify-center">
          <Button variant="secondary" size="sm" onClick={() => setLimit((v) => v + 100)}>
            {t.loadMore}
          </Button>
        </div>
      )}
    </div>
  )
}
