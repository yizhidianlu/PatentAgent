/**
 * 认证 react-query hooks（auth-system.md §5.1）：
 *
 * | 方法 | 路径 | 说明 |
 * |---|---|---|
 * | POST | `/auth/login` | `{username,password}` → `{user, csrf_token}`；失败 401 |
 * | POST | `/auth/logout` | 销毁会话 |
 * | GET  | `/auth/me` | 当前用户（含 quota / usage） |
 * | POST | `/auth/change-password` | `{old_password,new_password}`；400 = 旧密码错/新旧相同 |
 * | POST | `/auth/register` | 自助注册；**不签发会话**，账号落为 pending |
 * | GET  | `/auth/registration-open` | 是否开放注册（登录页据此显示入口） |
 *
 * 会话走 httpOnly Cookie，前端不保存任何凭据；CSRF 由 lib/api 自动带头。
 */

import { useEffect } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../lib/api'
import { useAuthStore } from '../stores/authStore'

// ---------------------------------------------------------------------------
// 类型（与 backend/app/models/auth.py 一一对应）
// ---------------------------------------------------------------------------

export type Role = 'admin' | 'user'
// pending = 自助注册后等待管理员审核；与 disabled（管理员停用）语义不同
export type UserStatus = 'active' | 'disabled' | 'pending'

/** 按用户的用量上限；0 表示不限。 */
export interface Quota {
  daily_llm_calls: number
  monthly_tokens: number
  storage_mb: number
  max_cases: number
}

/** 当前周期用量快照。 */
export interface UsageSnapshot {
  llm_calls_today: number
  tokens_this_month: number
  storage_mb: number
  case_count: number
}

export interface UserOut {
  id: string
  username: string
  display_name: string
  role: Role
  status: UserStatus
  must_change_password: boolean
  last_login_at: string | null
  locked_until: string | null
  quota: Quota
  usage: UsageSnapshot | null
  created_at: string
  updated_at: string
}

export interface LoginPayload {
  username: string
  password: string
}

export interface LoginResult {
  user: UserOut
  csrf_token: string
}

export interface ChangePasswordPayload {
  old_password: string
  new_password: string
}

export interface OkResult {
  ok?: boolean
  message?: string
}

export const EMPTY_QUOTA: Quota = {
  daily_llm_calls: 0,
  monthly_tokens: 0,
  storage_mb: 0,
  max_cases: 0,
}

export const authKeys = {
  me: ['auth', 'me'] as const,
}

/** 展示名回落用户名（后端 display_name 允许为空串）。 */
export function displayNameOf(user: Pick<UserOut, 'display_name' | 'username'> | null): string {
  if (!user) return ''
  return user.display_name.trim() || user.username
}

// ---------------------------------------------------------------------------
// hooks
// ---------------------------------------------------------------------------

/**
 * 当前登录用户。
 *
 * - `skipAuthHandling`：/auth/me 的 401 是**探测结果**而非异常，不能触发全局跳转
 *   （否则登录页自己会把自己踢一遍）；路由守卫按查询结果决定去向。
 * - `retry: false`：401 重试没有意义，只会拖慢首屏守卫。
 * - 顺带把结果镜像进 authStore，供头部 / 设置页等处同步读取。
 */
export function useMe() {
  const query = useQuery({
    queryKey: authKeys.me,
    queryFn: () => api.get<UserOut>('/auth/me', { skipAuthHandling: true }),
    retry: false,
    staleTime: 60_000,
    refetchOnWindowFocus: true,
  })

  const { data, isError } = query
  useEffect(() => {
    if (data) useAuthStore.getState().setUser(data)
    else if (isError) useAuthStore.getState().clear()
  }, [data, isError])

  return query
}

/** 登录；成功后把用户写进 query 缓存与 authStore（避免再等一次 /auth/me）。 */
export function useLogin() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: LoginPayload) =>
      api.post<LoginResult>('/auth/login', payload, { skipAuthHandling: true }),
    onSuccess: (data) => {
      queryClient.setQueryData(authKeys.me, data.user)
      useAuthStore.getState().setUser(data.user)
      // 换人登录后，上一位用户的案件 / 设置缓存必须全部作废
      void queryClient.invalidateQueries()
    },
  })
}

/**
 * 登出。
 *
 * 无论后端成败都清空本地缓存：登出按钮点了却还留在工作台里，比多清一次缓存危险得多。
 */
export function useLogout() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => api.post<OkResult>('/auth/logout'),
    onSettled: () => {
      useAuthStore.getState().clear()
      queryClient.clear()
    },
  })
}

export interface RegisterPayload {
  username: string
  password: string
  display_name?: string
}

export interface RegisterResult {
  ok: boolean
  status: UserStatus
  message: string
}

/** 是否开放自助注册。公开接口，未登录也能查。 */
export function useRegistrationOpen() {
  return useQuery({
    queryKey: ['auth', 'registration-open'],
    queryFn: () => api.get<{ open: boolean }>('/auth/registration-open', { skipAuthHandling: true }),
    staleTime: 60_000,
    retry: false,
  })
}

/**
 * 自助注册。
 *
 * 刻意不写 onSuccess 缓存用户——注册成功不等于登录成功，账号还在等审核。
 * 把它当登录处理会让人以为已经进去了。
 */
export function useRegister() {
  return useMutation({
    mutationFn: (payload: RegisterPayload) =>
      api.post<RegisterResult>('/auth/register', payload, { skipAuthHandling: true }),
  })
}

/** 修改密码；成功后刷新 /auth/me（must_change_password 会翻为 false）。 */
export function useChangePassword() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: ChangePasswordPayload) =>
      api.post<OkResult>('/auth/change-password', payload),
    onSuccess: () => {
      /*
       * 先就地把 must_change_password 落下来，再去 invalidate。
       * 只 invalidate 的话，调用方在 onSuccess 里 navigate('/') 时
       * /auth/me 还没回来，路由守卫看到的仍是「必须改密」，会把人原地弹回改密页——
       * 表现就是「提示改密成功，但页面纹丝不动」。服务端 200 已经代表标志位清了，
       * 这里同步一次并不越权。
       */
      const user = useAuthStore.getState().user
      if (user?.must_change_password) {
        const next: UserOut = { ...user, must_change_password: false }
        useAuthStore.getState().setUser(next)
        queryClient.setQueryData(authKeys.me, next)
      }
      void queryClient.invalidateQueries({ queryKey: authKeys.me })
    },
  })
}

/** 把任意异常规整为可直接展示的中文（后端 detail 已是中文文案）。 */
export function authErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError) return error.message || fallback
  if (error instanceof Error && error.message) return error.message
  return fallback
}
