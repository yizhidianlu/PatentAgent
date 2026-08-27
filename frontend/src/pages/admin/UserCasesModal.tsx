import { useNavigate } from 'react-router-dom'
import { ExclamationTriangleIcon } from '@heroicons/react/24/outline'
import { zh } from '../../i18n/zh'
import { formatDateTime } from '../../lib/format'
import { displayNameOf, type UserOut } from '../../api/auth'
import { useUserCases } from '../../api/admin'
import type { Module } from '../../types/models'
import { Badge } from '../../components/ui/Badge'
import { Button } from '../../components/ui/Button'
import { Modal } from '../../components/ui/Modal'
import { Skeleton } from '../../components/ui/Skeleton'

/** 模块 → 工作台路由前缀（与 routes/router.tsx 一致）。 */
const MODULE_PATH: Record<Module, string> = {
  disclosure: '/disclosure',
  paper2patent: '/paper',
  reader: '/reader',
  oa: '/oa',
}

export interface UserCasesModalProps {
  user: UserOut | null
  onClose: () => void
}

/** 查看某用户的案件列表；点「打开」进入工作台（后端会记 cross_user_read 审计）。 */
export function UserCasesModal({ user, onClose }: UserCasesModalProps) {
  const t = zh.admin.users.casesModal
  const navigate = useNavigate()
  const query = useUserCases(user?.id ?? null)
  const items = query.data?.items ?? []

  return (
    <Modal
      open={Boolean(user)}
      onClose={onClose}
      title={t.title(user ? displayNameOf(user) : '')}
      panelClassName="max-w-3xl"
    >
      <div className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-xs text-gray-500 dark:text-gray-400">
            {t.total(query.data?.total ?? 0)}
          </p>
          <p className="flex items-center gap-1.5 text-xs text-amber-600 dark:text-amber-400">
            <ExclamationTriangleIcon className="w-3.5 h-3.5 shrink-0" strokeWidth={1.5} />
            {t.auditNote}
          </p>
        </div>

        {query.isPending ? (
          <div className="space-y-2">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} shimmer className="h-10 rounded-lg" />
            ))}
          </div>
        ) : query.isError ? (
          <p className="py-6 text-center text-sm text-red-500">{t.loadFailed}</p>
        ) : items.length === 0 ? (
          <p className="py-8 text-center text-sm text-gray-500 dark:text-gray-400">{t.empty}</p>
        ) : (
          <div className="max-h-[50vh] overflow-auto rounded-xl border border-gray-200 dark:border-gray-700">
            <table className="w-full min-w-[36rem] text-sm">
              <thead className="sticky top-0 bg-gray-50 dark:bg-gray-900/60 backdrop-blur">
                <tr className="text-left text-xs text-gray-500 dark:text-gray-400">
                  <th className="px-3 py-2 font-medium">{t.columns.title}</th>
                  <th className="px-3 py-2 font-medium">{t.columns.module}</th>
                  <th className="px-3 py-2 font-medium">{t.columns.status}</th>
                  <th className="px-3 py-2 font-medium">{t.columns.updatedAt}</th>
                  <th className="px-3 py-2" />
                </tr>
              </thead>
              <tbody>
                {items.map((c) => (
                  <tr
                    key={c.id}
                    className="border-t border-gray-100 dark:border-gray-700/60 hover:bg-gray-50 dark:hover:bg-gray-700/30"
                  >
                    <td className="px-3 py-2 max-w-[16rem] truncate text-gray-800 dark:text-gray-100">
                      {c.title || zh.sidebar.untitled}
                    </td>
                    <td className="px-3 py-2">
                      <Badge>{zh.admin.stats.modules[c.module] ?? c.module}</Badge>
                    </td>
                    <td className="px-3 py-2 text-xs text-gray-500 dark:text-gray-400">
                      {c.status}
                    </td>
                    <td className="px-3 py-2 text-xs text-gray-500 dark:text-gray-400 whitespace-nowrap">
                      {formatDateTime(c.updated_at)}
                    </td>
                    <td className="px-3 py-2 text-right">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => {
                          onClose()
                          navigate(`${MODULE_PATH[c.module] ?? '/disclosure'}/${c.id}`)
                        }}
                      >
                        {t.open}
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </Modal>
  )
}
