import { useMemo, useState } from 'react'
import {
  ArrowPathIcon,
  EllipsisHorizontalIcon,
  FolderOpenIcon,
  KeyIcon,
  MagnifyingGlassIcon,
  NoSymbolIcon,
  PlusIcon,
  ShieldCheckIcon,
  TrashIcon,
  UserIcon,
  UsersIcon,
} from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { formatDateTime, formatRelativeTime } from '../../lib/format'
import { displayNameOf, type Role, type UserOut, type UserStatus } from '../../api/auth'
import {
  useAdminUsers,
  useResetUserPassword,
  useUpdateUser,
  type PasswordIssued,
} from '../../api/admin'
import { useCurrentUser } from '../../stores/authStore'
import { useUiStore } from '../../stores/uiStore'
import { Badge } from '../../components/ui/Badge'
import { Button } from '../../components/ui/Button'
import { Card } from '../../components/ui/Card'
import { Dropdown, DropdownItem } from '../../components/ui/Dropdown'
import { EmptyState } from '../../components/ui/EmptyState'
import { Input } from '../../components/ui/Input'
import { Modal } from '../../components/ui/Modal'
import { Select } from '../../components/ui/Select'
import { Skeleton } from '../../components/ui/Skeleton'
import { Spinner } from '../../components/ui/Spinner'
import { RoleBadge } from '../../components/layout/UserMenu'
import { CreateUserModal } from './CreateUserModal'
import { DeleteUserModal } from './DeleteUserModal'
import { OneTimePasswordModal } from './OneTimePasswordModal'
import { UserCasesModal } from './UserCasesModal'

const t = zh.admin.users

function StatusBadge({ status }: { status: UserStatus }) {
  return (
    <Badge variant={status === 'active' ? 'emerald' : 'neutral'}>
      {zh.auth.statuses[status] ?? status}
    </Badge>
  )
}

/** 千分位（tokens 动辄七位数，不分组根本读不出量级）。 */
function groupDigits(n: number): string {
  return n.toLocaleString('zh-CN')
}

/** 锁定是否仍然有效（后端只回 locked_until 时刻，过期与否要前端自己判）。 */
function isLocked(lockedUntil: string | null): boolean {
  if (!lockedUntil) return false
  const ts = new Date(lockedUntil).getTime()
  return Number.isFinite(ts) && ts > Date.now()
}

/** 用量单元格：案件数 / 今日调用 / 本月 tokens / 存储。 */
function UsageCell({ user }: { user: UserOut }) {
  const usage = user.usage
  if (!usage) return <span className="text-xs text-gray-400">{t.usageUnknown}</span>
  return (
    <div className="text-xs text-gray-500 dark:text-gray-400 space-y-0.5 whitespace-nowrap">
      <p>
        {t.usageCases(usage.case_count)} · {t.usageCalls(usage.llm_calls_today)}
      </p>
      <p>
        {t.usageTokens(groupDigits(usage.tokens_this_month))} ·{' '}
        {t.usageStorage(usage.storage_mb.toFixed(1))}
      </p>
    </div>
  )
}

export function AdminUsersPage() {
  const me = useCurrentUser()
  const pushToast = useUiStore((s) => s.pushToast)

  const [keyword, setKeyword] = useState('')
  const [role, setRole] = useState<Role | ''>('')
  const [status, setStatus] = useState<UserStatus | ''>('')

  const [createOpen, setCreateOpen] = useState(false)
  const [issued, setIssued] = useState<{ kind: 'created' | 'reset'; data: PasswordIssued } | null>(
    null,
  )
  const [resetTarget, setResetTarget] = useState<UserOut | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<UserOut | null>(null)
  const [casesTarget, setCasesTarget] = useState<UserOut | null>(null)

  const query = useAdminUsers({ q: keyword.trim(), role, status })
  const updateUser = useUpdateUser()
  const resetPassword = useResetUserPassword()

  const users = useMemo(() => query.data?.items ?? [], [query.data])
  const filtering = Boolean(keyword.trim() || role || status)

  const patch = (user: UserOut, changes: Parameters<typeof updateUser.mutate>[0]['patch']) => {
    updateUser.mutate(
      { id: user.id, patch: changes },
      {
        onSuccess: () => pushToast('success', t.updateOk),
        onError: (err) =>
          pushToast('error', err instanceof Error && err.message ? err.message : t.updateFailed),
      },
    )
  }

  const handleReset = () => {
    if (!resetTarget) return
    resetPassword.mutate(resetTarget.id, {
      onSuccess: (data) => {
        setResetTarget(null)
        setIssued({ kind: 'reset', data })
      },
      onError: (err) =>
        pushToast(
          'error',
          err instanceof Error && err.message ? err.message : t.resetConfirm.failed,
        ),
    })
  }

  return (
    <div className="max-w-6xl space-y-4">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-base font-semibold text-gray-900 dark:text-gray-100">{t.title}</h2>
          <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">{t.desc}</p>
        </div>
        <Button onClick={() => setCreateOpen(true)}>
          <PlusIcon className="w-4 h-4" strokeWidth={2} />
          {t.create}
        </Button>
      </header>

      {/* 搜索与筛选 */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative flex-1 min-w-[12rem]">
          <MagnifyingGlassIcon
            className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400"
            strokeWidth={1.5}
          />
          <Input
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            placeholder={t.searchPlaceholder}
            aria-label={t.searchPlaceholder}
            className="pl-9"
          />
        </div>
        <Select
          value={role}
          onChange={(e) => setRole(e.target.value as Role | '')}
          aria-label={t.filterRole}
          className="w-32"
        >
          <option value="">{`${t.filterRole}：${t.filterAll}`}</option>
          <option value="admin">{zh.auth.roles.admin}</option>
          <option value="user">{zh.auth.roles.user}</option>
        </Select>
        <Select
          value={status}
          onChange={(e) => setStatus(e.target.value as UserStatus | '')}
          aria-label={t.filterStatus}
          className="w-32"
        >
          <option value="">{`${t.filterStatus}：${t.filterAll}`}</option>
          <option value="active">{zh.auth.statuses.active}</option>
          <option value="disabled">{zh.auth.statuses.disabled}</option>
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
      </div>

      <Card className="overflow-hidden">
        {query.isPending ? (
          <div className="p-4 space-y-2">
            {[0, 1, 2, 3].map((i) => (
              <Skeleton key={i} shimmer className="h-12 rounded-lg" />
            ))}
          </div>
        ) : query.isError ? (
          <div className="p-8 text-center space-y-3">
            <p className="text-sm text-red-500">{zh.admin.loadFailed}</p>
            <Button variant="secondary" size="sm" onClick={() => void query.refetch()}>
              {zh.admin.reload}
            </Button>
          </div>
        ) : users.length === 0 ? (
          <EmptyState
            icon={UsersIcon}
            title={filtering ? t.searchEmptyTitle : t.emptyTitle}
            description={filtering ? t.searchEmptyDesc : t.emptyDesc}
            action={
              filtering ? undefined : (
                <Button size="sm" onClick={() => setCreateOpen(true)}>
                  {t.create}
                </Button>
              )
            }
          />
        ) : (
          <div className="overflow-x-auto">
            {/* min-w：窄屏改为横向滚动，而不是把角色/状态徽章挤成竖排单字 */}
            <table className="w-full min-w-[52rem] text-sm">
              <thead className="bg-gray-50 dark:bg-gray-900/40">
                <tr className="text-left text-xs text-gray-500 dark:text-gray-400 whitespace-nowrap">
                  <th className="px-4 py-2.5 font-medium">{t.columns.user}</th>
                  <th className="px-4 py-2.5 font-medium">{t.columns.role}</th>
                  <th className="px-4 py-2.5 font-medium">{t.columns.status}</th>
                  <th className="px-4 py-2.5 font-medium">{t.columns.lastLogin}</th>
                  <th className="px-4 py-2.5 font-medium">{t.columns.usage}</th>
                  <th className="px-4 py-2.5 font-medium text-right">{t.columns.actions}</th>
                </tr>
              </thead>
              <tbody>
                {users.map((user) => {
                  const isSelf = user.id === me?.id
                  const name = displayNameOf(user)
                  const locked = isLocked(user.locked_until)
                  return (
                    <tr
                      key={user.id}
                      className="border-t border-gray-100 dark:border-gray-700/60 hover:bg-gray-50/70 dark:hover:bg-gray-700/20"
                    >
                      <td className="px-4 py-3">
                        <p className="flex items-center gap-2 min-w-0">
                          <span className="font-medium text-gray-900 dark:text-gray-100 truncate max-w-[10rem]">
                            {name}
                          </span>
                          {isSelf && <Badge variant="indigo">{t.self}</Badge>}
                          {user.must_change_password && (
                            <Badge variant="amber">{t.mustChangePassword}</Badge>
                          )}
                        </p>
                        <p className="mt-0.5 text-xs text-gray-400 dark:text-gray-500">
                          @{user.username}
                        </p>
                        {locked && (
                          <p className="mt-0.5 text-xs text-red-500">
                            {t.lockedUntil(formatDateTime(user.locked_until as string))}
                          </p>
                        )}
                      </td>
                      <td className="px-4 py-3 whitespace-nowrap">
                        <RoleBadge role={user.role} />
                      </td>
                      <td className="px-4 py-3 whitespace-nowrap">
                        <StatusBadge status={user.status} />
                      </td>
                      <td className="px-4 py-3 text-xs text-gray-500 dark:text-gray-400 whitespace-nowrap">
                        {user.last_login_at ? formatRelativeTime(user.last_login_at) : t.neverLoggedIn}
                      </td>
                      <td className="px-4 py-3">
                        <UsageCell user={user} />
                      </td>
                      <td className="px-4 py-3 text-right">
                        <Dropdown
                          align="right"
                          panelClassName="w-52"
                          trigger={({ open, toggle }) => (
                            <button
                              type="button"
                              onClick={toggle}
                              aria-expanded={open}
                              aria-label={t.actions.label(name)}
                              className={cn(
                                'w-8 h-8 rounded-lg flex items-center justify-center text-gray-500 dark:text-gray-400',
                                'hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors',
                                open && 'bg-gray-100 dark:bg-gray-700',
                              )}
                            >
                              <EllipsisHorizontalIcon className="w-5 h-5" strokeWidth={1.5} />
                            </button>
                          )}
                        >
                          {({ close }) => (
                            <>
                              <DropdownItem
                                disabled={isSelf}
                                onClick={() => {
                                  close()
                                  patch(user, { role: user.role === 'admin' ? 'user' : 'admin' })
                                }}
                              >
                                {user.role === 'admin' ? (
                                  <UserIcon className="w-4 h-4 text-gray-400" strokeWidth={1.5} />
                                ) : (
                                  <ShieldCheckIcon className="w-4 h-4 text-gray-400" strokeWidth={1.5} />
                                )}
                                <span className="flex-1">
                                  {user.role === 'admin' ? t.actions.demote : t.actions.promote}
                                </span>
                              </DropdownItem>
                              <DropdownItem
                                disabled={isSelf}
                                onClick={() => {
                                  close()
                                  patch(user, {
                                    status: user.status === 'active' ? 'disabled' : 'active',
                                  })
                                }}
                              >
                                <NoSymbolIcon className="w-4 h-4 text-gray-400" strokeWidth={1.5} />
                                <span className="flex-1">
                                  {user.status === 'active' ? t.actions.disable : t.actions.enable}
                                </span>
                              </DropdownItem>
                              <DropdownItem
                                onClick={() => {
                                  close()
                                  setResetTarget(user)
                                }}
                              >
                                <KeyIcon className="w-4 h-4 text-gray-400" strokeWidth={1.5} />
                                <span className="flex-1">{t.actions.resetPassword}</span>
                              </DropdownItem>
                              <DropdownItem
                                onClick={() => {
                                  close()
                                  setCasesTarget(user)
                                }}
                              >
                                <FolderOpenIcon className="w-4 h-4 text-gray-400" strokeWidth={1.5} />
                                <span className="flex-1">{t.actions.viewCases}</span>
                              </DropdownItem>
                              <DropdownItem
                                danger
                                disabled={isSelf}
                                onClick={() => {
                                  close()
                                  setDeleteTarget(user)
                                }}
                              >
                                <TrashIcon className="w-4 h-4" strokeWidth={1.5} />
                                <span className="flex-1">{t.actions.delete}</span>
                              </DropdownItem>
                            </>
                          )}
                        </Dropdown>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {query.data && users.length > 0 && (
        <p className="text-xs text-gray-400 dark:text-gray-500">{t.total(query.data.total)}</p>
      )}

      <CreateUserModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={(data) => {
          setCreateOpen(false)
          setIssued({ kind: 'created', data })
        }}
      />

      <OneTimePasswordModal
        open={Boolean(issued)}
        kind={issued?.kind ?? 'created'}
        user={issued?.data.user ?? null}
        password={issued?.data.password ?? ''}
        note={issued?.data.note}
        onClose={() => setIssued(null)}
      />

      <UserCasesModal user={casesTarget} onClose={() => setCasesTarget(null)} />
      <DeleteUserModal user={deleteTarget} onClose={() => setDeleteTarget(null)} />

      {/* 重置密码二次确认 */}
      <Modal
        open={Boolean(resetTarget)}
        onClose={() => !resetPassword.isPending && setResetTarget(null)}
        title={t.resetConfirm.title}
      >
        <div className="space-y-4">
          <p className="text-sm text-gray-600 dark:text-gray-300">
            {t.resetConfirm.desc(resetTarget ? displayNameOf(resetTarget) : '')}
          </p>
          <div className="flex justify-end gap-2">
            <Button
              variant="ghost"
              onClick={() => setResetTarget(null)}
              disabled={resetPassword.isPending}
            >
              {zh.common.cancel}
            </Button>
            <Button onClick={handleReset} disabled={resetPassword.isPending}>
              {resetPassword.isPending ? (
                <>
                  <Spinner size="sm" className="border-white/40 border-t-white" />
                  {t.resetConfirm.submitting}
                </>
              ) : (
                t.resetConfirm.submit
              )}
            </Button>
          </div>
        </div>
      </Modal>
    </div>
  )
}
