import { memo, useEffect, useState, useSyncExternalStore } from 'react'
import { ExclamationTriangleIcon } from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'

// ---------------------------------------------------------------------------
// mermaid 动态加载（单例）
// ---------------------------------------------------------------------------

type MermaidApi = typeof import('mermaid').default

let mermaidPromise: Promise<MermaidApi> | null = null

function loadMermaid(): Promise<MermaidApi> {
  mermaidPromise ??= import('mermaid').then((m) => m.default)
  return mermaidPromise
}

let renderSeq = 0

// ---------------------------------------------------------------------------
// 暗色订阅（<html> class 变化 → 全量重渲，§2.7）
// ---------------------------------------------------------------------------

const darkListeners = new Set<() => void>()
let darkObserver: MutationObserver | null = null

function subscribeDark(listener: () => void): () => void {
  darkListeners.add(listener)
  if (!darkObserver && typeof MutationObserver !== 'undefined') {
    darkObserver = new MutationObserver(() => {
      for (const l of darkListeners) l()
    })
    darkObserver.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['class'],
    })
  }
  return () => {
    darkListeners.delete(listener)
    if (darkListeners.size === 0) {
      darkObserver?.disconnect()
      darkObserver = null
    }
  }
}

function isDarkSnapshot(): boolean {
  return document.documentElement.classList.contains('dark')
}

/** 订阅当前主题明暗（跟随 lib/theme.ts 写入的 html class）。 */
export function useIsDark(): boolean {
  return useSyncExternalStore(subscribeDark, isDarkSnapshot)
}

// ---------------------------------------------------------------------------
// 组件
// ---------------------------------------------------------------------------

export interface MermaidBlockProps {
  code: string
  /** 流式中围栏尚未闭合：只显示骨架，不尝试渲染（§2.7）。 */
  pending?: boolean
  className?: string
}

interface RenderState {
  status: 'loading' | 'done' | 'error'
  svg?: string
  message?: string
}

/** shimmer 骨架：「图表生成中…」。 */
function MermaidSkeleton() {
  return (
    <div
      className={cn(
        'relative my-4 h-40 rounded-xl border border-gray-200 dark:border-gray-700',
        'bg-gray-100/80 dark:bg-gray-800/60 overflow-hidden',
        'flex items-center justify-center',
      )}
      aria-busy="true"
    >
      <div
        className={cn(
          'absolute inset-0 animate-shimmer',
          'bg-gradient-to-r from-transparent via-white/60 to-transparent dark:via-white/10',
        )}
      />
      <span className="relative text-xs text-gray-500 dark:text-gray-400">
        {zh.markdown.mermaidPending}
      </span>
    </div>
  )
}

/**
 * §2.7 MermaidBlock：仅围栏闭合后渲染；
 * `mermaid.initialize({startOnLoad:false, theme: isDark?'dark':'default', securityLevel:'strict'})`；
 * 解析错误 → amber「图表语法待修正」+ 折叠源码；主题切换全量重渲。
 */
export const MermaidBlock = memo(function MermaidBlock({
  code,
  pending = false,
  className,
}: MermaidBlockProps) {
  const isDark = useIsDark()
  const [state, setState] = useState<RenderState>({ status: 'loading' })

  useEffect(() => {
    if (pending) return
    let cancelled = false
    setState({ status: 'loading' })

    const run = async (): Promise<void> => {
      const mermaid = await loadMermaid()
      if (cancelled) return
      mermaid.initialize({
        startOnLoad: false,
        theme: isDark ? 'dark' : 'default',
        securityLevel: 'strict',
        fontFamily: 'inherit',
      })
      renderSeq += 1
      const renderId = `pa-mermaid-${renderSeq}`
      try {
        const { svg } = await mermaid.render(renderId, code)
        if (!cancelled) setState({ status: 'done', svg })
      } catch (error) {
        // mermaid 渲染失败会在 body 残留错误占位节点，清掉
        document.getElementById(renderId)?.remove()
        document.getElementById(`d${renderId}`)?.remove()
        if (!cancelled) {
          setState({
            status: 'error',
            message: error instanceof Error ? error.message : String(error),
          })
        }
      }
    }

    void run()
    return () => {
      cancelled = true
    }
  }, [code, pending, isDark])

  if (pending || state.status === 'loading') return <MermaidSkeleton />

  if (state.status === 'error') {
    return (
      <div
        className={cn(
          'my-4 rounded-xl border border-amber-300 dark:border-amber-500/40',
          'bg-amber-50 dark:bg-amber-500/10 px-4 py-3',
          className,
        )}
      >
        <div className="flex items-center gap-2 text-sm text-amber-700 dark:text-amber-400">
          <ExclamationTriangleIcon className="w-4 h-4 shrink-0" strokeWidth={1.5} />
          {zh.markdown.mermaidError}
        </div>
        <details className="mt-2">
          <summary className="cursor-pointer text-xs text-amber-600/80 dark:text-amber-400/80 select-none">
            {zh.markdown.mermaidShowSource}
          </summary>
          <pre className="mt-2 rounded-lg bg-white/70 dark:bg-gray-900/60 p-3 text-xs overflow-x-auto text-gray-700 dark:text-gray-300">
            {code}
          </pre>
        </details>
      </div>
    )
  }

  return (
    <div
      className={cn(
        'my-4 bg-white dark:bg-gray-900 rounded-xl border border-gray-200 dark:border-gray-700',
        'p-4 overflow-x-auto scrollbar-thin [&_svg]:mx-auto [&_svg]:max-w-full',
        className,
      )}
      // securityLevel:'strict' 下 mermaid 已对输出做净化
      dangerouslySetInnerHTML={{ __html: state.svg ?? '' }}
    />
  )
})
