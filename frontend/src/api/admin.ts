/**
 * 管理后台 react-query hooks（auth-system.md §5.1 `/admin/*`，全部需管理员）：
 *
 * | 方法 | 路径 |
 * |---|---|
 * | GET | `/admin/users?q=&role=&status=&limit=&offset=` → `Page<UserOut>` |
 * | POST | `/admin/users` → **201** `{user, password, note}`（明文密码仅此一次） |
 * | PATCH / DELETE | `/admin/users/{id}` |
 * | POST | `/admin/users/{id}/reset-password` → `{user, password, note}` |
 * | GET | `/admin/users/{id}/cases` |
 * | GET | `/admin/audit?action=&actor_id=&limit=&offset=` |
 * | GET | `/admin/stats` |
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../lib/api'
import type { Quota, Role, UserOut, UserStatus } from './auth'
import type { Case, Page } from '../types/models'

// ---------------------------------------------------------------------------
// 类型
// ---------------------------------------------------------------------------

export interface UserListParams {
  q?: string
  role?: Role | ''
  status?: UserStatus | ''
  limit?: number
  offset?: number
}

export interface UserCreatePayload {
  username: string
  display_name: string
  role: Role
  /** 省略 = 由服务端生成随机强密码。 */
  password?: string
  quota?: Quota
}

export interface UserUpdatePayload {
  display_name?: string
  role?: Role
  status?: UserStatus
  quota?: Quota
}

/** 建号 / 重置密码的一次性密码响应。 */
export interface PasswordIssued {
  user: UserOut
  password: string
  note: string
}

/** 审计日志中出现的 action 取值（后端可能新增，故保留 string 兜底）。 */
export type AuditAction =
  | 'login'
  | 'login_failed'
  | 'logout'
  | 'user_create'
  | 'user_update'
  | 'user_delete'
  | 'password_reset'
  | 'password_change'
  | 'cross_user_read'
  | 'settings_update'
  | 'case_delete'

export interface AuditEntry {
  id: number
  actor_id: string | null
  actor_name: string | null
  action: AuditAction | string
  target_type: string | null
  target_id: string | null
  target_owner: string | null
  detail: Record<string, unknown>
  ip: string | null
  created_at: string
}

export interface AuditParams {
  action?: string
  actor_id?: string
  limit?: number
  offset?: number
}

export interface AdminStats {
  user_count: number
  active_user_count: number
  case_count: number
  case_count_by_module: Record<string, number>
  llm_calls_today: number
  tokens_this_month: number
  storage_mb: number
  artifact_count: number
}

/** 后端 /admin/users/{id}/cases 返回的裸分页（字段与 Page<Case> 一致）。 */
export type UserCasesPage = Page<Case>

export const adminKeys = {
  all: ['admin'] as const,
  users: (params: UserListParams) => ['admin', 'users', params] as const,
  usersAll: ['admin', 'users'] as const,
  userCases: (userId: string) => ['admin', 'user-cases', userId] as const,
  audit: (params: AuditParams) => ['admin', 'audit', params] as const,
  auditAll: ['admin', 'audit'] as const,
  stats: ['admin', 'stats'] as const,
}

// ---------------------------------------------------------------------------
// 用户
// ---------------------------------------------------------------------------

/**
 * 用户列表。`enabled=false` 时完全不发请求 —— 跨用户提示条会在**任意**用户的
 * 工作台里挂载，普通用户打这个管理员端点只会白白吃一串 403。
 */
export function useAdminUsers(params: UserListParams = {}, options: { enabled?: boolean } = {}) {
  const { q = '', role = '', status = '', limit = 100, offset = 0 } = params
  const { enabled = true } = options
  return useQuery({
    queryKey: adminKeys.users({ q, role, status, limit, offset }),
    queryFn: () =>
      api.get<Page<UserOut>>('/admin/users', {
        query: { q, role, status, limit, offset },
      }),
    enabled,
    staleTime: 15_000,
    placeholderData: (prev) => prev,
  })
}

export function useCreateUser() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: UserCreatePayload) =>
      api.post<PasswordIssued>('/admin/users', payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: adminKeys.usersAll })
      void queryClient.invalidateQueries({ queryKey: adminKeys.stats })
      void queryClient.invalidateQueries({ queryKey: adminKeys.auditAll })
    },
  })
}

export function useUpdateUser() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: UserUpdatePayload }) =>
      api.patch<UserOut>(`/admin/users/${encodeURIComponent(id)}`, patch),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: adminKeys.usersAll })
      void queryClient.invalidateQueries({ queryKey: adminKeys.auditAll })
    },
  })
}

export function useResetUserPassword() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) =>
      api.post<PasswordIssued>(`/admin/users/${encodeURIComponent(id)}/reset-password`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: adminKeys.usersAll })
      void queryClient.invalidateQueries({ queryKey: adminKeys.auditAll })
    },
  })
}

export function useDeleteUser() {
  const queryClient = useQueryClient()
  return useMutation({
    // 默认不删盘：磁盘上的原始材料与交付物不可再生，而备份对已删文件只保留有限窗口
    mutationFn: ({ id, purgeFiles = false }: { id: string; purgeFiles?: boolean }) =>
      api.delete<{ ok?: boolean; message?: string }>(
        `/admin/users/${encodeURIComponent(id)}`,
        { query: { purge_files: purgeFiles } },
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: adminKeys.usersAll })
      void queryClient.invalidateQueries({ queryKey: adminKeys.stats })
      void queryClient.invalidateQueries({ queryKey: adminKeys.auditAll })
    },
  })
}

/** 某用户的案件列表（enabled=false 时不发请求，供 Modal 懒加载）。 */
export function useUserCases(userId: string | null) {
  return useQuery({
    queryKey: adminKeys.userCases(userId ?? ''),
    queryFn: () =>
      api.get<UserCasesPage>(`/admin/users/${encodeURIComponent(userId ?? '')}/cases`, {
        query: { limit: 200 },
      }),
    enabled: Boolean(userId),
    staleTime: 15_000,
  })
}

// ---------------------------------------------------------------------------
// 审计与统计
// ---------------------------------------------------------------------------

export function useAuditLog(params: AuditParams = {}) {
  const { action = '', actor_id = '', limit = 100, offset = 0 } = params
  return useQuery({
    queryKey: adminKeys.audit({ action, actor_id, limit, offset }),
    queryFn: () =>
      api.get<Page<AuditEntry>>('/admin/audit', {
        query: { action, actor_id, limit, offset },
      }),
    staleTime: 10_000,
    placeholderData: (prev) => prev,
  })
}

export interface RegistrationPolicy {
  allow_registration: boolean
  pending_count: number
}

/** 自助注册策略 + 待审数量。待审数量要挂在后台入口上，否则没人想起来去看。 */
export function useRegistrationPolicy() {
  return useQuery({
    queryKey: ['admin', 'registration'],
    queryFn: () => api.get<RegistrationPolicy>('/admin/registration'),
    staleTime: 30_000,
  })
}

export function useSetRegistrationPolicy() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (allow: boolean) =>
      api.put<RegistrationPolicy>('/admin/registration', { allow_registration: allow }),
    onSuccess: (data) => {
      queryClient.setQueryData(['admin', 'registration'], data)
    },
  })
}

export function useAdminStats() {
  return useQuery({
    queryKey: adminKeys.stats,
    queryFn: () => api.get<AdminStats>('/admin/stats'),
    staleTime: 30_000,
  })
}
