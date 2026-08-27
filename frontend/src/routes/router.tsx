import { Suspense, lazy, type ReactNode } from 'react'
import { Navigate, createBrowserRouter, type RouteObject } from 'react-router-dom'
import { AppLayout } from '../components/layout/AppLayout'
import { SecondaryLayout } from '../components/layout/SecondaryLayout'
import { HomePage } from '../pages/HomePage'
import { DisclosurePage } from '../pages/DisclosurePage'
import { PaperPage } from '../pages/PaperPage'
import { ReaderPage } from '../pages/ReaderPage'
import { OAPage } from '../pages/OAPage'
import { OACasesPage } from '../pages/OACasesPage'
import { SettingsPage } from '../pages/SettingsPage'
import { NotFoundPage } from '../pages/NotFoundPage'
import { LoginPage } from '../pages/LoginPage'
import { ChangePasswordPage } from '../pages/ChangePasswordPage'
import { AdminPage } from '../pages/AdminPage'
import { AdminUsersPage } from '../pages/admin/AdminUsersPage'
import { AdminAuditPage } from '../pages/admin/AdminAuditPage'
import { AdminStatsPage } from '../pages/admin/AdminStatsPage'
import { RequireAdmin, RequireAuth, RequirePasswordSet } from './guards'
import { Spinner } from '../components/ui/Spinner'

function LazyRoute({ children }: { children: ReactNode }) {
  return (
    <Suspense
      fallback={
        <div className="flex-1 flex items-center justify-center py-20">
          <Spinner size="lg" />
        </div>
      }
    >
      {children}
    </Suspense>
  )
}

function buildDevRoutes(): RouteObject[] {
  const DesignSystemPage = lazy(() =>
    import('../pages/DesignSystemPage').then((m) => ({ default: m.DesignSystemPage })),
  )
  const StreamDemoPage = lazy(() =>
    import('../pages/StreamDemoPage').then((m) => ({ default: m.StreamDemoPage })),
  )
  return [
    {
      element: <SecondaryLayout />,
      children: [
        {
          path: '/design-system',
          element: (
            <LazyRoute>
              <DesignSystemPage />
            </LazyRoute>
          ),
        },
        {
          path: '/stream-demo',
          element: (
            <LazyRoute>
              <StreamDemoPage />
            </LazyRoute>
          ),
        },
      ],
    },
  ]
}

/**
 * §3 路由表（M8 起整棵树套在认证守卫内）：
 *
 * /login                             公开
 * ── RequireAuth                      未登录 → /login
 *    /change-password                 强制改密（must_change_password 时的唯一落点）
 *    ── RequirePasswordSet            must_change_password → /change-password
 *       /                             AppLayout → HomePage
 *       /disclosure/:id …             四模块工作台
 *       /oa/cases /settings           SecondaryLayout
 *       ── RequireAdmin
 *          /admin/{users,audit,stats} 管理后台
 *       /design-system /stream-demo   dev-only（lazy）
 */
const protectedRoutes: RouteObject[] = [
  {
    element: <AppLayout />,
    children: [
      { path: '/', element: <HomePage /> },
      { path: '/disclosure/:id', element: <DisclosurePage /> },
      { path: '/paper/:id', element: <PaperPage /> },
      { path: '/reader/:id', element: <ReaderPage /> },
      { path: '/oa/:id', element: <OAPage /> },
    ],
  },
  {
    element: <SecondaryLayout />,
    children: [
      { path: '/oa/cases', element: <OACasesPage /> },
      { path: '/settings', element: <SettingsPage /> },
    ],
  },
  {
    // RequireAdmin 套在 SecondaryLayout 之内：无权限页也要带头部与页面底色，
    // 否则普通用户手敲 /admin 会掉进一块没有导航、退不出去的裸页面
    element: <SecondaryLayout />,
    children: [
      {
        element: <RequireAdmin />,
        children: [
          {
            path: '/admin',
            element: <AdminPage />,
            children: [
              { index: true, element: <Navigate to="/admin/users" replace /> },
              { path: 'users', element: <AdminUsersPage /> },
              { path: 'audit', element: <AdminAuditPage /> },
              { path: 'stats', element: <AdminStatsPage /> },
            ],
          },
        ],
      },
    ],
  },
  /**
   * dev-only 页面（/design-system、/stream-demo）：
   * 两页静态引用 mocks/mockServer 与整套脚本化时间线，直接 import 会把
   * mock 数据打进生产主 bundle。这里同时做两件事——
   * ① React.lazy 动态引入，把它们从主 bundle 拆出去；
   * ② lazy() 调用写在 `import.meta.env.DEV` 分支内部（而非模块顶层），
   *    生产构建把该常量折为 false 后整段是死代码，连 chunk 都不会产出。
   * 顶层写 lazy() 时 Rollup 会把它当作有副作用的调用而保留 chunk，故必须内联。
   */
  ...(import.meta.env.DEV ? buildDevRoutes() : []),
  {
    element: <SecondaryLayout />,
    children: [{ path: '*', element: <NotFoundPage /> }],
  },
]

const routes: RouteObject[] = [
  { path: '/login', element: <LoginPage /> },
  {
    element: <RequireAuth />,
    children: [
      // 改密页必须在 RequirePasswordSet 之外，否则「必须改密」的用户会被
      // 无限重定向到一个自己也进不去的页面
      { path: '/change-password', element: <ChangePasswordPage /> },
      { element: <RequirePasswordSet />, children: protectedRoutes },
    ],
  },
]

export const router = createBrowserRouter(routes)
