import { useMemo, useState } from 'react'
import {
  ArrowTopRightOnSquareIcon,
  ExclamationTriangleIcon,
  GlobeAltIcon,
  PlusIcon,
  XMarkIcon,
} from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { Badge } from '../ui/Badge'
import { Button } from '../ui/Button'
import { Input } from '../ui/Input'
import { StageCardShell, type StageCardProps } from '../pipeline/StageCardShell'
import type { InteractionRequiredEvent } from '../../types/stream'
import { asRecord, pickArray, pickBool, pickStr, readDefault, strList } from './stagePayload'

/** 查新命中条目（prior_art/abstract_digest.md：pub_number / title / applicant / link / abstract）。 */
interface PriorArtHit {
  id: string
  pubNumber: string
  title: string
  url: string
  applicant: string
  date: string
  abstract: string
  manual?: boolean
}

function readHits(payload: InteractionRequiredEvent | null): PriorArtHit[] {
  const defaults = readDefault(payload)
  const raw = pickArray(defaults, 'hits', 'results', 'prior_art', 'items')
  return raw.map((item, i) => {
    const record = asRecord(item)
    const pubNumber = pickStr(record, 'pub_number', 'pub_no', 'publication_number', 'number')
    return {
      id: pickStr(record, 'id', 'hit_id') || pubNumber || String(i + 1),
      pubNumber,
      title: pickStr(record, 'title', 'name'),
      url: pickStr(record, 'link', 'url', 'source_url'),
      applicant: pickStr(record, 'applicant', 'assignee', 'applicants'),
      date: pickStr(record, 'date', 'pub_date', 'publication_date', 'published_at'),
      abstract: pickStr(record, 'abstract', 'summary', 'digest'),
    }
  })
}

const EMPTY_MANUAL = { pub_number: '', title: '', url: '', abstract: '' }

/**
 * §2.8 模块 A / A4 联网查新确认卡：
 * 命中列表（外链标题 + 公开号/日期/申请人 meta + 摘要 line-clamp-3 可展开 + 逐条纳入勾选）、
 * 「手动添加现有技术」内联表单、「跳过查新」与「确认继续」。
 * 空结果 / 查新失败时手动补录表单前置显著展示（A4 失败分支：禁止编造检索结果）。
 */
export function PriorArtCard({ stage, submit, skip, busy }: StageCardProps) {
  const payload = stage.payload as InteractionRequiredEvent | null
  const hits = useMemo(() => readHits(payload), [payload])
  const defaults = readDefault(payload)
  const failed = pickBool(defaults, ['failed', 'search_failed', 'error'], false)
  const failureMessage = pickStr(defaults, 'error_message', 'message')

  const resultIds = strList(asRecord(stage.result)?.hit_ids)
  const [selected, setSelected] = useState<string[]>(() =>
    resultIds.length > 0 ? resultIds : hits.map((h) => h.id),
  )
  const [expanded, setExpanded] = useState<string[]>([])
  const [manualHits, setManualHits] = useState<PriorArtHit[]>([])
  // 空结果 / 失败态：手动补录表单默认展开并前置
  const [manualOpen, setManualOpen] = useState(() => hits.length === 0 || failed)
  const [draft, setDraft] = useState({ ...EMPTY_MANUAL })
  const [draftError, setDraftError] = useState<string | null>(null)

  const allHits = [...hits, ...manualHits]

  const toggle = (id: string) =>
    setSelected((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]))
  const toggleAbstract = (id: string) =>
    setExpanded((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]))

  const addManual = () => {
    if (!draft.pub_number.trim() && !draft.title.trim()) {
      setDraftError(zh.stages.priorArt.manualIncomplete)
      return
    }
    const entry: PriorArtHit = {
      id: `manual-${manualHits.length + 1}-${Date.now()}`,
      pubNumber: draft.pub_number.trim(),
      title: draft.title.trim(),
      url: draft.url.trim(),
      applicant: '',
      date: '',
      abstract: draft.abstract.trim(),
      manual: true,
    }
    setManualHits((prev) => [...prev, entry])
    setSelected((prev) => [...prev, entry.id])
    setDraft({ ...EMPTY_MANUAL })
    setDraftError(null)
  }

  const removeManual = (id: string) => {
    setManualHits((prev) => prev.filter((h) => h.id !== id))
    setSelected((prev) => prev.filter((x) => x !== id))
  }

  const manualForm = (
    <div className="rounded-xl border border-gray-200 dark:border-gray-700 p-3 space-y-2">
      <div className="flex items-center justify-between gap-2">
        <p className="text-xs font-medium text-gray-600 dark:text-gray-300">
          {zh.stages.priorArt.manualTitle}
        </p>
        <button
          type="button"
          onClick={() => setManualOpen(false)}
          className="text-[11px] text-gray-400 hover:text-gray-600 dark:hover:text-gray-200"
        >
          {zh.stages.priorArt.manualCancel}
        </button>
      </div>
      <div className="grid sm:grid-cols-2 gap-2">
        <Input
          value={draft.pub_number}
          placeholder={zh.stages.priorArt.manualPubNo}
          onChange={(e) => setDraft((p) => ({ ...p, pub_number: e.target.value }))}
        />
        <Input
          value={draft.title}
          placeholder={zh.stages.priorArt.manualName}
          onChange={(e) => setDraft((p) => ({ ...p, title: e.target.value }))}
        />
      </div>
      <Input
        value={draft.url}
        placeholder={zh.stages.priorArt.manualUrl}
        onChange={(e) => setDraft((p) => ({ ...p, url: e.target.value }))}
      />
      <textarea
        value={draft.abstract}
        placeholder={zh.stages.priorArt.manualAbstract}
        onChange={(e) => setDraft((p) => ({ ...p, abstract: e.target.value }))}
        className={cn(
          'w-full min-h-[72px] rounded-xl border border-gray-300 dark:border-gray-600',
          'bg-white dark:bg-gray-800 px-3.5 py-2.5 text-sm text-gray-900 dark:text-gray-100',
          'placeholder:text-gray-400 outline-none transition-colors scrollbar-thin',
          'focus:border-[#6366f1] focus:ring-4 focus:ring-indigo-500/10',
        )}
      />
      {draftError && <p className="text-[11px] text-red-500">{draftError}</p>}
      <div className="flex justify-end">
        <Button size="sm" variant="secondary" className="gap-1" onClick={addManual}>
          <PlusIcon className="w-3.5 h-3.5" strokeWidth={2} />
          {zh.stages.priorArt.manualAdd}
        </Button>
      </div>
    </div>
  )

  return (
    <StageCardShell
      stage={stage}
      icon={GlobeAltIcon}
      title={zh.stage.kinds.prior_art}
      summary={zh.stages.priorArt.summary(
        resultIds.length > 0 ? resultIds.length : selected.length,
      )}
      onConfirm={() =>
        submit({
          hit_ids: selected,
          manual: manualHits
            .filter((h) => selected.includes(h.id))
            .map((h) => ({
              pub_number: h.pubNumber,
              title: h.title,
              url: h.url,
              abstract: h.abstract,
            })),
          skipped: false,
        })
      }
      onSkip={skip}
      confirmLabel={zh.stages.priorArt.confirm}
      skipLabel={zh.stages.priorArt.skip}
      busy={busy}
    >
      <div className="space-y-3">
        {payload?.prompt && (
          <p className="text-sm text-gray-700 dark:text-gray-300">{payload.prompt}</p>
        )}

        {/* 空结果 / 失败：告警条 + 前置手动补录 */}
        {(hits.length === 0 || failed) && (
          <div
            className={cn(
              'rounded-xl border px-3 py-2.5 flex items-start gap-2 text-xs',
              'border-amber-200 dark:border-amber-500/30 bg-amber-50 dark:bg-amber-500/10',
              'text-amber-700 dark:text-amber-400',
            )}
            role="status"
          >
            <ExclamationTriangleIcon className="w-4 h-4 shrink-0 mt-0.5" strokeWidth={1.5} />
            <span className="min-w-0">
              <span className="block font-medium">
                {failed ? zh.stages.priorArt.failed : zh.stages.priorArt.empty}
              </span>
              <span className="block mt-0.5 leading-relaxed opacity-90">
                {failureMessage || zh.stages.priorArt.emptyHint}
              </span>
            </span>
          </div>
        )}
        {(hits.length === 0 || failed) && manualOpen && manualForm}

        {allHits.length > 0 && (
          <>
            <p className="text-xs text-gray-500 dark:text-gray-400">
              {zh.stages.priorArt.hitsTitle(allHits.length)} ·{' '}
              {zh.stages.priorArt.selectedCount(selected.length, allHits.length)}
            </p>

            <ul className="space-y-2">
              {allHits.map((hit) => {
                const active = selected.includes(hit.id)
                const open = expanded.includes(hit.id)
                const meta = [hit.pubNumber, hit.date, hit.applicant].filter(Boolean).join(' · ')
                return (
                  <li
                    key={hit.id}
                    className={cn(
                      'rounded-xl border px-3 py-2.5 transition-colors',
                      active
                        ? 'border-indigo-300 dark:border-indigo-500/50 bg-indigo-50/40 dark:bg-indigo-500/5'
                        : 'border-gray-200 dark:border-gray-700',
                    )}
                  >
                    <div className="flex items-start gap-2.5">
                      <input
                        type="checkbox"
                        checked={active}
                        onChange={() => toggle(hit.id)}
                        aria-label={`${zh.stages.priorArt.include}：${hit.title || hit.pubNumber}`}
                        className="mt-0.5 w-4 h-4 shrink-0 rounded border-gray-300 dark:border-gray-600 accent-[#6366f1]"
                      />
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-1.5 flex-wrap">
                          {hit.url ? (
                            <a
                              href={hit.url}
                              target="_blank"
                              rel="noreferrer noopener"
                              title={zh.stages.priorArt.openLink}
                              className="text-sm font-medium text-[#6366f1] dark:text-indigo-400 hover:underline inline-flex items-center gap-1 min-w-0"
                            >
                              <span className="truncate">{hit.title || hit.pubNumber}</span>
                              <ArrowTopRightOnSquareIcon className="w-3 h-3 shrink-0" strokeWidth={1.5} />
                            </a>
                          ) : (
                            <span className="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">
                              {hit.title || hit.pubNumber}
                            </span>
                          )}
                          {hit.manual && (
                            <Badge variant="neutral">{zh.stages.priorArt.manualBadge}</Badge>
                          )}
                          {hit.manual && (
                            <button
                              type="button"
                              onClick={() => removeManual(hit.id)}
                              aria-label={zh.stages.priorArt.manualRemove}
                              title={zh.stages.priorArt.manualRemove}
                              className="text-gray-400 hover:text-red-500 transition-colors"
                            >
                              <XMarkIcon className="w-3.5 h-3.5" strokeWidth={2} />
                            </button>
                          )}
                        </div>
                        {meta && (
                          <p className="mt-0.5 text-[11px] text-gray-400 dark:text-gray-500 truncate">
                            {meta}
                          </p>
                        )}
                        <p
                          className={cn(
                            'mt-1 text-xs leading-relaxed text-gray-600 dark:text-gray-300 whitespace-pre-wrap break-words',
                            !open && 'line-clamp-3',
                          )}
                        >
                          {hit.abstract || zh.stages.priorArt.noAbstract}
                        </p>
                        {hit.abstract.length > 60 && (
                          <button
                            type="button"
                            onClick={() => toggleAbstract(hit.id)}
                            className="mt-0.5 text-[11px] text-[#6366f1] dark:text-indigo-400 hover:underline"
                          >
                            {open
                              ? zh.stages.priorArt.collapseAbstract
                              : zh.stages.priorArt.expandAbstract}
                          </button>
                        )}
                      </div>
                    </div>
                  </li>
                )
              })}
            </ul>
          </>
        )}

        {/* 常规态的手动补录入口 */}
        {hits.length > 0 && !failed && (
          manualOpen ? (
            manualForm
          ) : (
            <button
              type="button"
              onClick={() => setManualOpen(true)}
              className={cn(
                'inline-flex items-center gap-1 text-xs font-medium',
                'text-gray-500 dark:text-gray-400 hover:text-[#6366f1] dark:hover:text-indigo-400',
                'transition-colors',
              )}
            >
              <PlusIcon className="w-3.5 h-3.5" strokeWidth={2} />
              {zh.stages.priorArt.manualToggle}
            </button>
          )
        )}
      </div>
    </StageCardShell>
  )
}
