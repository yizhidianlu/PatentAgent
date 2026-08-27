import { useCallback, useEffect, useMemo, useRef, useState, type RefObject } from 'react'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import type { ReaderSection } from '../../api/reader'

/** 报告正文中每节的锚点属性（ReportView 写入，ToC 依此定位）。 */
export const SECTION_ATTR = 'data-report-section'

export interface ReportTocProps {
  sections: ReaderSection[]
  /** 报告滚动容器（IntersectionObserver root + 点击滚动目标）。 */
  scrollerRef: RefObject<HTMLElement | null>
  className?: string
}

/**
 * §3.4 报告目录侧栏：`w-44 shrink-0 text-[13px] space-y-1`；
 * 项 `px-2 py-1.5 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 cursor-pointer`，
 * active `bg-indigo-50 text-[#6366f1] font-medium dark:bg-indigo-500/10`；
 * IntersectionObserver 做 scroll-spy（无标题的前言块不进目录）。
 */
export function ReportToc({ sections, scrollerRef, className }: ReportTocProps) {
  const entries = useMemo(
    () => sections.filter((s) => s.title.trim() !== '').map((s) => ({ id: s.id, title: s.title })),
    [sections],
  )
  const idKey = entries.map((e) => e.id).join('|')

  const [activeId, setActiveId] = useState<string | null>(null)
  const visibleRef = useRef<Set<string>>(new Set())

  useEffect(() => {
    const root = scrollerRef.current
    if (!root || entries.length === 0) return
    const order = new Map(entries.map((e, i) => [e.id, i]))
    const visible = visibleRef.current

    const observer = new IntersectionObserver(
      (records) => {
        for (const record of records) {
          const id = record.target.getAttribute(SECTION_ATTR)
          if (!id) continue
          if (record.isIntersecting) visible.add(id)
          else visible.delete(id)
        }
        if (visible.size === 0) return
        let best: string | null = null
        let bestIndex = Number.POSITIVE_INFINITY
        for (const id of visible) {
          const index = order.get(id) ?? Number.POSITIVE_INFINITY
          if (index < bestIndex) {
            bestIndex = index
            best = id
          }
        }
        if (best) setActiveId(best)
      },
      // 顶部 ~12% 处为判定线：滚到某节标题即点亮该节
      { root, rootMargin: '0px 0px -78% 0px', threshold: 0 },
    )

    for (const entry of entries) {
      const el = root.querySelector(`[${SECTION_ATTR}="${entry.id}"]`)
      if (el) observer.observe(el)
    }
    return () => {
      observer.disconnect()
      visible.clear()
    }
    // entries 内容变化经 idKey 体现（流式期间节会逐个出现）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [idKey, scrollerRef])

  const jumpTo = useCallback(
    (id: string) => {
      const root = scrollerRef.current
      const el = root?.querySelector(`[${SECTION_ATTR}="${id}"]`)
      setActiveId(id)
      el?.scrollIntoView({ block: 'start', behavior: 'smooth' })
    },
    [scrollerRef],
  )

  if (entries.length === 0) return null

  return (
    <nav
      aria-label={zh.reader.panel.tocLabel}
      className={cn('w-44 shrink-0 text-[13px] space-y-1', className)}
    >
      {entries.map((entry) => (
        <button
          key={entry.id}
          type="button"
          onClick={() => jumpTo(entry.id)}
          aria-current={activeId === entry.id ? 'true' : undefined}
          className={cn(
            'w-full text-left px-2 py-1.5 rounded-lg cursor-pointer transition-colors truncate',
            activeId === entry.id
              ? 'bg-indigo-50 text-[#6366f1] font-medium dark:bg-indigo-500/10 dark:text-indigo-400'
              : 'text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800',
          )}
          title={entry.title}
        >
          {entry.title}
        </button>
      ))}
    </nav>
  )
}
