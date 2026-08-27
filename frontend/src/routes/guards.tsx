import { useEffect } from 'react'
import { Navigate, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { NoSymbolIcon } from '@heroicons/react/24/outline'
import { zh } from '../i18n/zh'
import { setApiAuthHandler } from '../lib/api'
import { authKeys, useMe } from '../api/auth'
import { useAuthStore } from '../stores/authStore'
import { Button } from '../components/ui/Button'
import { EmptyState } from '../components/ui/EmptyState'
import { Spinner } from '../components/ui/Spinner'

/**
 * 路由守卫（auth-system.md §6）：
 *
 *   RequireAuth            未登录 → /login（记住原去处）
 *     └ RequirePasswordSet must_change_password → /change-password
 *         └ RequireAdmin   role!=='admin' → 无权限页（不是 404，也不重定向，
 *                          让管理员知道自己确实点到了受限页）
 *
 * 守卫期间显示全屏 loading，避免「先闪一眼工作台再跳登录页」。
 */

/** 全屏加载态（守卫判定中）。 */
export function FullScreenLoading({ label = zh.auth.guard.checking }: { label?: string }) {
  return (
    <div className="min-h-screen w-full flex flex-col items-center justify-center gap-3 bg-gradient-to-br from-slate-50 via-blue-50/30 to-indigo-50/50 dark:from-gray-900 dark:via-gray-800/30 dark:to-slate-900/50">
      <img src="/logo.svg?v=4" alt={zh.brand.logoAlt} className="w-10 h-10 opacity-80" />
      <Spinner size="lg" />
      <p className="text-xs text-gray-500 dark:text-gray-400">{label}</p>
    </div>
  )
}

/**
 * 把 lib/api 的全局认证失败回调接到 react-router 上。
 *
 * 挂在守卫外层：401 时用 navigate 而不是整页刷新，既保住 SPA 状态，
 * 又能把「原本要去哪」带到登录页。
 */
function useApiAuthBridge(): void {
  const navigate = useNavigate()
  const location = useLocation()
  const queryClient = useQueryClient()

  useEffect(() => {
    return setApiAuthHandler((kind) => {
      if (kind === 'unauthorized') {
        // 清本地状态：留着上一位用户的缓存会让登录页背后闪出别人的数据
        useAuthStore.getState().clear()
        queryClient.setQueryData(authKeys.me, null)
        queryClient.clear()
        if (window.location.pathname !== '/login') {
          navigate('/login', { replace: true, state: { from: location.pathname + location.search } })
        }
        return
      }
      if (window.location.pathname !== '/change-password') {
        navigate('/change-password', { replace: true })
      }
    })
  }, [navigate, location.pathname, location.search, queryClient])
}

/** 已登录才放行；未登录跳 /login 并记住原去处。 */
export function RequireAuth() {
  const location = useLocation()
  const query = useMe()
  const user = useAuthStore((s) => s.user)
  useApiAuthBridge()

  if (query.isPending) return <FullScreenLoading />
  if (query.isError || !query.data) {
    return (
      <Navigate
        to="/login"
        replace
        state={{ from: location.pathname + location.search }}
      />
    )
  }
  /*
   * 多等一帧 store 镜像同步完再放行。
   * useMe 是在 effect 里把用户写进 authStore 的，若拿到 data 就立刻渲染子树，
   * 下游（RequirePasswordSet / RequireAdmin / 设置页分岔）会先看到 user=null，
   * 于是「必须改密」的用户能瞥见一帧工作台、管理员会先闪一下普通用户设置页。
   */
  if (!user) return <FullScreenLoading />
  return <Outlet />
}

/** must_change_password 时把任何路由都拦到 /change-password。 */
export function RequirePasswordSet() {
  const user = useAuthStore((s) => s.user)
  if (user?.must_change_password) return <Navigate to="/change-password" replace />
  return <Outlet />
}

/** 无权限页（/admin/* 的非管理员落点）。 */
export function ForbiddenPage() {
  const navigate = useNavigate()
  return (
    <div className="flex-1 min-h-0 flex items-center justify-center p-6">
      <EmptyState
        icon={NoSymbolIcon}
        title={zh.auth.guard.forbiddenTitle}
        description={zh.auth.guard.forbiddenDesc}
        action={
          <Button variant="secondary" size="sm" onClick={() => navigate('/')}>
            {zh.auth.guard.backHome}
          </Button>
        }
      />
    </div>
  )
}

/**
 * 管理员专属区。前端隐藏只是体验，后端 require_admin 才是真正的闸门。
 * 挂在布局之内，故加载态用行内 spinner（而非全屏），不撑破页面。
 */
export function RequireAdmin() {
  const user = useAuthStore((s) => s.user)
  if (!user) {
    return (
      <div className="flex-1 flex items-center justify-center py-20">
        <Spinner size="lg" />
      </div>
    )
  }
  if (user.role !== 'admin') return <ForbiddenPage />
  return <Outlet />
}
