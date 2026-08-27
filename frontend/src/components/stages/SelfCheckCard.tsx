import { useEffect, useMemo, useState } from 'react'
import {
  CheckCircleIcon,
  CheckIcon,
  ExclamationTriangleIcon,
  ShieldCheckIcon,
} from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { Spinner } from '../ui/Spinner'
import { StageCardShell, type StageCardProps } from '../pipeline/StageCardShell'
import type { InteractionRequiredEvent } from '../../types/stream'
import { asRecord, num, pickArray, pickStr, readDefault, str, strList } from './stagePayload'

/** 逐项完成的动画节拍（ms）。 */
const TICK_MS = 520

interface CheckItem {
  id: string
  label: string
  note: string
}

function readItems(payload: InteractionRequiredEvent | null): CheckItem[] {
  const d = readDefault(payload)
  const raw = pickArray(d, 'items', 'checks', 'checklist', 'results')
  return raw.map((item, i) => {
    const record = asRecord(item)
    return {
      id: pickStr(record, 'id', 'key', 'checklist_item') || String(i + 1),
      label:
        pickStr(record, 'label', 'title', 'name', 'checklist_item', 'item') || str(item) || `#${i + 1}`,
      note: pickStr(record, 'note', 'detail', 'reason', 'result'),
    }
  })
}

/**
 * §2.8 模块 A / A7 自检卡：只读检查项列表（Spinner → CheckIcon 逐项完成），
 * 全部完成后自动折叠为「自检完成，已自动修订 N 处」，unresolved 项单独提示人工复核。
 */
export function SelfCheckCard({ stage, submit, skip, busy }: StageCardProps) {
  const payload = stage.payload as InteractionRequiredEvent | null
  const items = useMemo(() => readItems(payload), [payload])
  const defaults = readDefault(payload)

  const patchCount = useMemo(() => {
    const explicit = num(defaults.patch_count ?? defaults.patches_applied)
    if (explicit !== null) return explicit
    return pickArray(defaults, 'patches', 'applied_patches').length
  }, [defaults])
  const unresolved = useMemo(
    () => strList(defaults.unresolved ?? defaults.manual_review),
    [defaults],
  )

  const settled = stage.status !== 'active'
  const [tick, setTick] = useState(() => (settled ? items.length : 0))
  const [expanded, setExpanded] = useState(false)

  // 逐项完成动画（卡片已定格时直接按全完成渲染）
  useEffect(() => {
    if (settled || items.length === 0) return
    const timer = setInterval(() => {
      setTick((prev) => {
        const next = prev + 1
        if (next >= items.length) clearInterval(timer)
        return Math.min(next, items.length)
      })
    }, TICK_MS)
    return () => clearInterval(timer)
  }, [items.length, settled])

  const done = settled ? items.length : tick
  const finished = items.length === 0 || done >= items.length
  const showList = !finished || expanded

  return (
    <StageCardShell
      stage={stage}
      icon={ShieldCheckIcon}
      title={zh.stage.kinds.self_check}
      summary={zh.stages.selfCheck.summary(patchCount)}
      onConfirm={() => submit({ acknowledged: true, patch_count: patchCount })}
      onSkip={skip}
      confirmLabel={zh.stages.selfCheck.confirm}
      confirmDisabled={!finished}
      hideSkip
      busy={busy}
    >
      <div className="space-y-3">
        {payload?.prompt && (
          <p className="text-sm text-gray-700 dark:text-gray-300">{payload.prompt}</p>
        )}

        {items.length === 0 ? (
          <p className="text-sm text-gray-400 dark:text-gray-500">{zh.stages.selfCheck.empty}</p>
        ) : finished ? (
          <div className="flex items-center gap-2">
            <CheckCircleIcon className="w-4 h-4 shrink-0 text-emerald-500" strokeWidth={1.5} />
            <span className="text-sm text-gray-700 dark:text-gray-300 flex-1 min-w-0">
              {zh.stages.selfCheck.doneTitle(patchCount)}
            </span>
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              className="shrink-0 text-[11px] text-[#6366f1] dark:text-indigo-400 hover:underline"
            >
              {expanded ? zh.stages.selfCheck.detailsCollapse : zh.stages.selfCheck.detailsToggle}
            </button>
          </div>
        ) : (
          <p className="text-xs text-gray-500 dark:text-gray-400">{zh.stages.selfCheck.running}</p>
        )}

        {showList && items.length > 0 && (
          <ul className="space-y-1.5">
            {items.map((item, i) => {
              const complete = i < done
              return (
                <li key={item.id} className="flex items-start gap-2">
                  <span className="w-4 h-4 mt-0.5 shrink-0 flex items-center justify-center">
                    {complete ? (
                      <CheckIcon className="w-4 h-4 text-emerald-500" strokeWidth={2.5} />
                    ) : (
                      <Spinner size="sm" className="w-3.5 h-3.5" />
                    )}
                  </span>
                  <span className="min-w-0">
                    <span
                      className={cn(
                        'block text-xs leading-relaxed',
                        complete
                          ? 'text-gray-600 dark:text-gray-300'
                          : 'text-gray-400 dark:text-gray-500',
                      )}
                    >
                      {item.label}
                    </span>
                    {complete && item.note && (
                      <span className="block text-[11px] text-gray-400 dark:text-gray-500">
                        {item.note}
                      </span>
                    )}
                  </span>
                </li>
              )
            })}
          </ul>
        )}

        {finished && unresolved.length > 0 && (
          <div
            className={cn(
              'rounded-xl border px-3 py-2.5 text-xs',
              'border-amber-200 dark:border-amber-500/30 bg-amber-50 dark:bg-amber-500/10',
              'text-amber-700 dark:text-amber-400',
            )}
          >
            <p className="font-medium flex items-center gap-1.5">
              <ExclamationTriangleIcon className="w-3.5 h-3.5 shrink-0" strokeWidth={1.5} />
              {zh.stages.selfCheck.unresolvedTitle}
            </p>
            <ul className="mt-1 space-y-0.5 list-disc list-inside leading-relaxed">
              {unresolved.map((text, i) => (
                <li key={i}>{text}</li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </StageCardShell>
  )
}
