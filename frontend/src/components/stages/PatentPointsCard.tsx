import { useMemo, useState } from 'react'
import { ChevronDownIcon, LightBulbIcon } from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { Badge } from '../ui/Badge'
import { StageCardShell, type StageCardProps } from '../pipeline/StageCardShell'
import type { InteractionRequiredEvent } from '../../types/stream'
import {
  asRecord,
  num,
  pickArray,
  pickStr,
  readDefault,
  str,
  strList,
} from './stagePayload'

/** 候选专利点（invention/points.md 契约：id/title/background/innovation/distinction/feasibility/score）。 */
interface PointCandidate {
  id: string
  title: string
  background: string
  innovation: string
  distinction: string
  feasibility: string
  score: number | null
}

function readCandidates(payload: InteractionRequiredEvent | null): PointCandidate[] {
  const defaults = readDefault(payload)
  const raw = pickArray(defaults, 'candidates', 'points', 'items')
  return raw.map((item, i) => {
    const record = asRecord(item)
    return {
      id: pickStr(record, 'id', 'key', 'point_id') || String(i + 1),
      title: pickStr(record, 'title', 'name', 'label') || str(item) || `#${i + 1}`,
      background: pickStr(record, 'background', 'context'),
      innovation: pickStr(record, 'innovation', 'innovations', 'novelty'),
      distinction: pickStr(record, 'distinction', 'difference', 'distinctions'),
      feasibility: pickStr(record, 'feasibility', 'implementability'),
      score: num(record?.score),
    }
  })
}

function scoreVariant(score: number): 'emerald' | 'indigo' | 'amber' {
  if (score >= 85) return 'emerald'
  if (score >= 70) return 'indigo'
  return 'amber'
}

/**
 * §2.8 模块 A / A3 专利点挖掘卡：
 * 候选点勾选列表（每条可展开 背景 / 创新点 / 与现有技术区别 / 可实施性 + score 徽章）、
 * 多选时可勾「融合为一个技术方案」，提交 `{selected_ids, fuse, fusion_note}`。
 */
export function PatentPointsCard({ stage, submit, skip, busy }: StageCardProps) {
  const payload = stage.payload as InteractionRequiredEvent | null
  const candidates = useMemo(() => readCandidates(payload), [payload])

  const defaults = readDefault(payload)
  const recommended = useMemo(() => {
    const raw = defaults.recommended ?? defaults.recommend
    return new Set(typeof raw === 'string' ? [raw] : strList(raw))
  }, [defaults])
  const fusionNote = pickStr(defaults, 'fusion_note', 'fusionNote')

  const resultIds = strList(asRecord(stage.result)?.selected_ids)
  const [selected, setSelected] = useState<string[]>(() => {
    if (resultIds.length > 0) return resultIds
    const rec = candidates.filter((c) => recommended.has(c.id)).map((c) => c.id)
    return rec.length > 0 ? rec : candidates.slice(0, 1).map((c) => c.id)
  })
  const [expanded, setExpanded] = useState<string[]>([])
  const [fuse, setFuse] = useState(false)

  const toggleSelect = (id: string) =>
    setSelected((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]))
  const toggleExpand = (id: string) =>
    setExpanded((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]))

  const detailRow = (label: string, value: string) =>
    value ? (
      <div className="flex gap-2">
        <span className="shrink-0 w-24 text-[11px] font-medium text-gray-400 dark:text-gray-500">
          {label}
        </span>
        <span className="text-xs leading-relaxed text-gray-600 dark:text-gray-300 whitespace-pre-wrap break-words">
          {value}
        </span>
      </div>
    ) : null

  return (
    <StageCardShell
      stage={stage}
      icon={LightBulbIcon}
      title={zh.stage.kinds.patent_points}
      summary={zh.stages.patentPoints.summary(
        resultIds.length > 0 ? resultIds.length : selected.length,
      )}
      onConfirm={() =>
        submit({
          selected_ids: selected,
          selected_titles: candidates.filter((c) => selected.includes(c.id)).map((c) => c.title),
          fuse: fuse && selected.length > 1,
          fusion_note: fuse ? fusionNote : '',
        })
      }
      onSkip={skip}
      confirmLabel={zh.stages.patentPoints.confirm}
      confirmDisabled={selected.length === 0}
      busy={busy}
    >
      <div className="space-y-3">
        {payload?.prompt && (
          <p className="text-sm text-gray-700 dark:text-gray-300">{payload.prompt}</p>
        )}

        {candidates.length === 0 ? (
          <p className="text-sm text-gray-400 dark:text-gray-500">{zh.stages.patentPoints.empty}</p>
        ) : (
          <>
            <p className="text-xs text-gray-500 dark:text-gray-400">
              {zh.stages.patentPoints.selectedCount(selected.length, candidates.length)}
            </p>

            <ul className="space-y-2">
              {candidates.map((point) => {
                const active = selected.includes(point.id)
                const open = expanded.includes(point.id)
                return (
                  <li
                    key={point.id}
                    className={cn(
                      'rounded-xl border transition-colors',
                      active
                        ? 'border-indigo-300 dark:border-indigo-500/50 bg-indigo-50/40 dark:bg-indigo-500/5'
                        : 'border-gray-200 dark:border-gray-700',
                    )}
                  >
                    <div className="flex items-start gap-2.5 px-3 py-2.5">
                      <input
                        type="checkbox"
                        checked={active}
                        onChange={() => toggleSelect(point.id)}
                        aria-label={point.title}
                        className="mt-0.5 w-4 h-4 shrink-0 rounded border-gray-300 dark:border-gray-600 accent-[#6366f1]"
                      />
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-1.5 flex-wrap">
                          <span className="text-sm font-medium text-gray-900 dark:text-gray-100">
                            {point.title}
                          </span>
                          {recommended.has(point.id) && (
                            <Badge variant="indigo">{zh.stages.patentPoints.recommended}</Badge>
                          )}
                          {point.score !== null && (
                            <Badge variant={scoreVariant(point.score)}>
                              {zh.stages.patentPoints.scoreLabel(point.score)}
                            </Badge>
                          )}
                        </div>
                        {!open && point.innovation && (
                          <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400 line-clamp-2">
                            {point.innovation}
                          </p>
                        )}
                      </div>
                      <button
                        type="button"
                        onClick={() => toggleExpand(point.id)}
                        aria-label={
                          open ? zh.stages.patentPoints.collapse : zh.stages.patentPoints.expand
                        }
                        title={open ? zh.stages.patentPoints.collapse : zh.stages.patentPoints.expand}
                        className="shrink-0 p-1 rounded-lg text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 transition-colors"
                      >
                        <ChevronDownIcon
                          className={cn('w-4 h-4 transition-transform duration-200', open && 'rotate-180')}
                          strokeWidth={1.5}
                        />
                      </button>
                    </div>

                    {open && (
                      <div className="px-3 pb-3 pt-0.5 space-y-1.5 border-t border-gray-100 dark:border-gray-700/60 mt-0.5">
                        <div className="pt-2 space-y-1.5">
                          {detailRow(zh.stages.patentPoints.fields.background, point.background)}
                          {detailRow(zh.stages.patentPoints.fields.innovation, point.innovation)}
                          {detailRow(zh.stages.patentPoints.fields.distinction, point.distinction)}
                          {detailRow(zh.stages.patentPoints.fields.feasibility, point.feasibility)}
                        </div>
                      </div>
                    )}
                  </li>
                )
              })}
            </ul>

            {/* 多选融合 */}
            {selected.length > 1 && (
              <div className="rounded-xl border border-gray-200 dark:border-gray-700 px-3 py-2.5">
                <label className="flex items-start gap-2.5 cursor-pointer select-none">
                  <input
                    type="checkbox"
                    checked={fuse}
                    onChange={(e) => setFuse(e.target.checked)}
                    className="mt-0.5 w-4 h-4 shrink-0 rounded border-gray-300 dark:border-gray-600 accent-[#6366f1]"
                  />
                  <span className="min-w-0">
                    <span className="block text-sm text-gray-800 dark:text-gray-200">
                      {zh.stages.patentPoints.fuseLabel}
                    </span>
                    <span className="block text-[11px] text-gray-400 dark:text-gray-500 mt-0.5">
                      {zh.stages.patentPoints.fuseHint}
                    </span>
                  </span>
                </label>
                {fuse && fusionNote && (
                  <p className="mt-2 pl-6 text-xs leading-relaxed text-gray-500 dark:text-gray-400">
                    <span className="font-medium text-gray-600 dark:text-gray-300">
                      {zh.stages.patentPoints.fusionNote}：
                    </span>
                    {fusionNote}
                  </p>
                )}
              </div>
            )}
          </>
        )}
      </div>
    </StageCardShell>
  )
}
