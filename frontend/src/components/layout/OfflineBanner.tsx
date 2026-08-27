import { useCallback, useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { useQueryClient } from '@tanstack/react-query'
import { ExclamationTriangleIcon } from '@heroicons/react/24/outline'
import { ApiError } from '../../lib/api'
import { zh } from '../../i18n/zh'

/** ApiError(status=0) = fetch 本身失败（后端未启动 / 端口不通 / 网络断开）。 */
function isBackendDown(error: unknown): boolean {
  return error instanceof ApiError && error.status === 0
}

/**
 * 全局「后端未启动」提示条：
 * 订阅 react-query 的 query / mutation 缓存事件——任一请求以 ApiError(status=0) 失败
 * 即判定后端不可达并置顶提示（含「重试」= 重新拉取全部查询）；任一请求成功即自动消隐。
 * 不额外轮询，纯被动观测，避免后端未启动时反复打无效请求。
 */
export function OfflineBanner() {
  const queryClient = useQueryClient()
  const [down, setDown] = useState(false)
  const [retrying, setRetrying] = useState(false)

  useEffect(() => {
    const queryCache = queryClient.getQueryCache()
    const mutationCache = queryClient.getMutationCache()

    const inspect = (state: { status?: string; error?: unknown } | undefined) => {
      if (!state) return
      if (state.status === 'error' && isBackendDown(state.error)) setDown(true)
      else if (state.status === 'success') setDown(false)
    }

    const unsubQuery = queryCache.subscribe((event) => inspect(event.query?.state))
    const unsubMutation = mutationCache.subscribe((event) => inspect(event.mutation?.state))

    // 挂载瞬间可能已有失败的查询（首屏并发请求早于本组件订阅）
    for (const query of queryCache.getAll()) inspect(query.state)

    return () => {
      unsubQuery()
      unsubMutation()
    }
  }, [queryClient])

  const retry = useCallback(() => {
    setRetrying(true)
    void queryClient
      .refetchQueries({ type: 'all' })
      .finally(() => setRetrying(false))
  }, [queryClient])

  if (!down) return null

  return createPortal(
    // 顶部居中浮出的胶囊而非整条横幅：整条 fixed 横幅会盖住固定侧栏顶部的
    // 「新建会话」按钮（侧栏 z-40 低于提示条），居中胶囊在 ≥768px 时正好落在
    // 主内容区上方，窄屏无侧栏亦不遮挡。
    <div
      role="alert"
      className={
        'fixed top-[calc(var(--app-header-h)+0.75rem)] left-1/2 -translate-x-1/2 ' +
        'z-[var(--z-toast)] max-w-[calc(100vw-2rem)] ' +
        'flex items-center gap-2 rounded-2xl px-4 py-2 text-xs leading-relaxed shadow-lg ' +
        'border border-red-200 dark:border-red-500/40 bg-red-50 dark:bg-red-950 ' +
        'text-red-700 dark:text-red-300 animate-in fade-in slide-in-from-top-2 duration-200'
      }
    >
      <ExclamationTriangleIcon className="w-4 h-4 shrink-0" strokeWidth={1.5} />
      <span className="min-w-0">{zh.connection.backendDown}</span>
      <button
        type="button"
        onClick={retry}
        disabled={retrying}
        className="shrink-0 font-medium underline underline-offset-2 hover:no-underline disabled:opacity-60"
      >
        {retrying ? zh.connection.backendRetrying : zh.common.retry}
      </button>
    </div>,
    document.body,
  )
}
