import { useCallback, useMemo, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { ChevronDownIcon, LinkIcon } from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { Badge } from '../ui/Badge'
import { buildClaimTree, claimTreeStats } from '../../api/reader'
import type { ClaimTreeData, ClaimTreeNode } from '../../api/reader'

export interface ClaimTreeProps {
  tree: ClaimTreeData
  /** 顶部「共 N 项 · 独立 x / 从属 y」导语。 */
  showSummary?: boolean
  /** 默认展开全文的权项（缺省全部折叠）。 */
  defaultExpanded?: number[]
  className?: string
}

/**
 * §2.11 权利要求树：递归渲染。
 * - 独权 `border-indigo-200 dark:border-indigo-500/40 bg-indigo-50/50 dark:bg-indigo-500/10 font-medium`，从权白底；
 * - 头行「权利要求 {n}」+ 类型徽章 + 一行白话增量 truncate；点击展开全文（AnimatePresence height auto）；
 * - 子级 `ml-4 pl-4 border-l border-gray-300 dark:border-gray-600 space-y-2 mt-2`；
 * - 多引（「权1或2」）在主引用之外渲染重复链接 chips，点击定位并高亮被引权项。
 */
export function ClaimTree({
  tree,
  showSummary = false,
  defaultExpanded,
  className,
}: ClaimTreeProps) {
  const roots = useMemo(() => buildClaimTree(tree.claims), [tree.claims])
  const stats = useMemo(() => claimTreeStats(tree.claims), [tree.claims])

  const [expanded, setExpanded] = useState<Set<number>>(() => new Set(defaultExpanded ?? []))
  const [highlighted, setHighlighted] = useState<number | null>(null)
  const nodeRefs = useRef(new Map<number, HTMLDivElement | null>())
  const highlightTimer = useRef<number | null>(null)

  const toggle = useCallback((no: number) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(no)) next.delete(no)
      else next.add(no)
      return next
    })
  }, [])

  /** 多引 chip：定位到被引权项并短暂高亮。 */
  const focusClaim = useCallback((no: number) => {
    const el = nodeRefs.current.get(no)
    if (!el) return
    el.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
    setHighlighted(no)
    if (highlightTimer.current !== null) window.clearTimeout(highlightTimer.current)
    highlightTimer.current = window.setTimeout(() => setHighlighted(null), 1600)
  }, [])

  const registerNode = useCallback((no: number, el: HTMLDivElement | null) => {
    nodeRefs.current.set(no, el)
  }, [])

  if (tree.claims.length === 0) {
    return (
      <p className={cn('text-sm text-gray-400 dark:text-gray-500', className)}>
        {zh.reader.claimTree.empty}
      </p>
    )
  }

  return (
    <div className={className}>
      {showSummary && (
        <p className="mb-2 text-xs text-gray-500 dark:text-gray-400">
          {zh.reader.claimTree.summary(stats.total, stats.independent, stats.dependent)}
        </p>
      )}
      <div className="space-y-2">
        {roots.map((node) => (
          <ClaimTreeItem
            key={node.claim.no}
            node={node}
            expanded={expanded}
            highlighted={highlighted}
            onToggle={toggle}
            onFocusClaim={focusClaim}
            registerNode={registerNode}
          />
        ))}
      </div>
    </div>
  )
}

interface ClaimTreeItemProps {
  node: ClaimTreeNode
  expanded: Set<number>
  highlighted: number | null
  onToggle: (no: number) => void
  onFocusClaim: (no: number) => void
  registerNode: (no: number, el: HTMLDivElement | null) => void
}

function ClaimTreeItem({
  node,
  expanded,
  highlighted,
  onToggle,
  onFocusClaim,
  registerNode,
}: ClaimTreeItemProps) {
  const { claim, children, extraRefs } = node
  const independent = claim.type === 'independent'
  const open = expanded.has(claim.no)
  const hasDetail = claim.preamble.trim() !== '' || claim.features.length > 0

  return (
    <div>
      <div
        ref={(el) => {
          registerNode(claim.no, el)
        }}
        data-claim-no={claim.no}
        className={cn(
          'rounded-lg border px-3 py-2 text-sm transition-colors',
          independent
            ? 'border-indigo-200 dark:border-indigo-500/40 bg-indigo-50/50 dark:bg-indigo-500/10 font-medium'
            : 'border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800',
          highlighted === claim.no && 'ring-4 ring-indigo-500/20 border-[#6366f1]',
        )}
      >
        <button
          type="button"
          onClick={() => onToggle(claim.no)}
          aria-expanded={open}
          className="w-full flex items-center gap-2 text-left outline-none"
        >
          <span className="shrink-0 text-gray-900 dark:text-gray-100">
            {zh.reader.claimTree.claimLabel(claim.no)}
          </span>
          <Badge variant={independent ? 'indigo' : 'neutral'} className="shrink-0">
            {independent ? zh.reader.claimTree.independent : zh.reader.claimTree.dependent}
          </Badge>
          <span
            className="flex-1 min-w-0 truncate text-[13px] font-normal text-gray-500 dark:text-gray-400"
            title={claim.delta || undefined}
          >
            {claim.delta || zh.reader.claimTree.noDelta}
          </span>
          {hasDetail && (
            <ChevronDownIcon
              className={cn(
                'w-4 h-4 shrink-0 text-gray-400 transition-transform duration-200',
                open && 'rotate-180',
              )}
              strokeWidth={1.5}
              aria-label={open ? zh.reader.claimTree.collapse : zh.reader.claimTree.expand}
            />
          )}
        </button>

        {/* 多引：主引用之外的重复链接 chips（「权1或2」） */}
        {extraRefs.length > 0 && (
          <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
            <span className="text-[11px] text-gray-400 dark:text-gray-500">
              {zh.reader.claimTree.alsoRefs}
            </span>
            {extraRefs.map((ref) => (
              <button
                key={ref}
                type="button"
                onClick={() => onFocusClaim(ref)}
                className={cn(
                  'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-normal',
                  'border border-indigo-200 dark:border-indigo-500/40 text-[#6366f1] dark:text-indigo-400',
                  'hover:bg-indigo-50 dark:hover:bg-indigo-500/10 transition-colors',
                )}
              >
                <LinkIcon className="w-3 h-3" strokeWidth={1.5} />
                {zh.reader.claimTree.refChip(ref)}
              </button>
            ))}
          </div>
        )}

        <AnimatePresence initial={false}>
          {open && hasDetail && (
            <motion.div
              key="detail"
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: 'auto', opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.2, ease: [0.25, 0.1, 0.25, 1] }}
              className="overflow-hidden"
            >
              <div className="pt-2 mt-2 border-t border-gray-200/70 dark:border-gray-700/70 space-y-2 font-normal">
                {claim.preamble.trim() !== '' && (
                  <p className="text-[13px] leading-relaxed text-gray-700 dark:text-gray-300">
                    <span className="text-gray-400 dark:text-gray-500 mr-1.5">
                      {zh.reader.claimTree.preamble}
                    </span>
                    {claim.preamble}
                  </p>
                )}
                {claim.features.length > 0 && (
                  <ul className="space-y-1">
                    {claim.features.map((feature) => (
                      <li
                        key={feature.fid}
                        className="flex gap-2 text-[13px] leading-relaxed text-gray-700 dark:text-gray-300"
                      >
                        <span className="shrink-0 font-mono text-[11px] text-gray-400 dark:text-gray-500 pt-0.5">
                          {feature.fid}
                        </span>
                        <span className="min-w-0">{feature.text}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      {children.length > 0 && (
        <div className="ml-4 pl-4 border-l border-gray-300 dark:border-gray-600 space-y-2 mt-2">
          {children.map((child) => (
            <ClaimTreeItem
              key={child.claim.no}
              node={child}
              expanded={expanded}
              highlighted={highlighted}
              onToggle={onToggle}
              onFocusClaim={onFocusClaim}
              registerNode={registerNode}
            />
          ))}
        </div>
      )}
    </div>
  )
}
