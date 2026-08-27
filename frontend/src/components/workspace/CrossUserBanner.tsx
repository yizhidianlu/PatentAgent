import { EyeIcon } from '@heroicons/react/24/outline'
import { zh } from '../../i18n/zh'
import { displayNameOf } from '../../api/auth'
import { useAdminUsers } from '../../api/admin'
import { useCurrentUser } from '../../stores/authStore'
import { Badge } from '../ui/Badge'
import type { CaseDetail } from '../../types/models'

/**
 * 管理员查看他人案件时的置顶提示条（auth-system.md §4.2）。
 *
 * 判定只依赖案件详情里的属主 id：普通用户拿不到他人案件（后端一律 404），
 * 所以能走到「属主 ≠ 自己」这一支的必然是管理员。
 * 属主名优先用后端下发的 owner_username / owner_display_name；
 * 只有 id 时再从 /admin/users 里查（管理员本就有这个权限，且结果会被缓存）。
 */
export function CrossUserBanner({ caseDetail }: { caseDetail: CaseDetail | undefined }) {
  const me = useCurrentUser()
  const ownerId = caseDetail?.user_id ?? caseDetail?.owner_id ?? null
  const isAdmin = me?.role === 'admin'
  const crossUser = Boolean(isAdmin && ownerId && me && ownerId !== me.id)

  // 仅在确实跨用户、且后端没给名字时才去查用户表
  const needsLookup = crossUser && !caseDetail?.owner_username && !caseDetail?.owner_display_name
  const usersQuery = useAdminUsers({ limit: 200 }, { enabled: needsLookup })
  const owner = (usersQuery.data?.items ?? []).find((u) => u.id === ownerId)

  if (!crossUser) return null

  const ownerName =
    caseDetail?.owner_display_name ||
    caseDetail?.owner_username ||
    (owner ? displayNameOf(owner) : '') ||
    zh.auth.crossUser.unknownOwner

  return (
    <div
      role="status"
      className="shrink-0 flex items-center gap-2 px-4 py-2.5 border-b border-amber-200 dark:border-amber-500/30 bg-amber-50 dark:bg-amber-500/10 text-xs sm:text-sm font-medium text-amber-800 dark:text-amber-300"
    >
      <EyeIcon className="w-4 h-4 shrink-0" strokeWidth={1.8} />
      <span className="flex-1 min-w-0">{zh.auth.crossUser.banner(ownerName)}</span>
      <Badge variant="amber" className="shrink-0">
        {zh.auth.crossUser.readOnly}
      </Badge>
    </div>
  )
}
