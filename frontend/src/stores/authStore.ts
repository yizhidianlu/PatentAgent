/**
 * 当前登录用户的全局状态（auth-system.md §6）。
 *
 * 真正的数据源是 react-query 的 `GET /auth/me`（见 api/auth.ts），本 store 只是
 * 一份供任意组件同步读取的镜像 —— 头部用户菜单、设置页分岔、跨用户提示条都要读
 * `role`，全都各自发一次 useMe() 既啰嗦又会在守卫之外制造额外请求。
 *
 * 纪律：只有 api/auth.ts 里的 hooks 负责写这份镜像，其余地方一律只读。
 */

import { create } from 'zustand'
import type { UserOut } from '../api/auth'

export type AuthStatus = 'unknown' | 'authenticated' | 'anonymous'

export interface AuthState {
  /** 当前用户；未登录 / 尚未探测完成为 null。 */
  user: UserOut | null
  /** unknown = /auth/me 还没有结论（守卫期间显示全屏 loading）。 */
  status: AuthStatus
  setUser: (user: UserOut) => void
  clear: () => void
}

export const useAuthStore = create<AuthState>((set) => ({
  user: null,
  status: 'unknown',
  setUser: (user) => set({ user, status: 'authenticated' }),
  clear: () => set({ user: null, status: 'anonymous' }),
}))

/** 当前用户（组件内只读）。 */
export function useCurrentUser(): UserOut | null {
  return useAuthStore((s) => s.user)
}

/** 是否管理员。前端隐藏只是体验，后端对每个管理员端点都有兜底鉴权。 */
export function useIsAdmin(): boolean {
  return useAuthStore((s) => (s.user?.role ?? 'user') === 'admin')
}

/** 非组件上下文读取（api 层 / 事件回调）。 */
export function currentUser(): UserOut | null {
  return useAuthStore.getState().user
}

export function isAdminUser(): boolean {
  return (useAuthStore.getState().user?.role ?? 'user') === 'admin'
}
