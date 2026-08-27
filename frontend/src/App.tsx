import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider } from 'react-router-dom'
import { MotionConfig } from 'framer-motion'
import { router } from './routes/router'
import { ToastViewport } from './components/ui/Toast'
import { OfflineBanner } from './components/layout/OfflineBanner'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
      staleTime: 30_000,
    },
  },
})

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      {/* reducedMotion="user"：系统开启「减弱动态效果」时，framer-motion 只保留
          opacity/颜色过渡，自动跳过 transform/width/height 等大幅位移动画。
          CSS 关键帧侧的降级见 styles/index.css 的 prefers-reduced-motion 块。 */}
      <MotionConfig reducedMotion="user">
        <RouterProvider router={router} />
        <OfflineBanner />
        <ToastViewport />
      </MotionConfig>
    </QueryClientProvider>
  )
}
