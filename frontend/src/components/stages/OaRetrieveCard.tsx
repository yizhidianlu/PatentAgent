import { useMemo, useState } from 'react'
import { CheckIcon, CircleStackIcon, MagnifyingGlassIcon } from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { outcomeBadgeVariant, scorePercent } from '../../lib/oaBadges'
import { Badge } from '../ui/Badge'
import { EmptyState } from '../ui/EmptyState'
import { StageCardShell, type StageCardProps } from '../pipeline/StageCardShell'
import { asRecord, num, pickArray, pickStr, readDefault, str, strList } from './stagePayload'
import type { InteractionRequiredEvent } from '../../types/stream'

/** 一条命中案例（OaSearchHit）。 */
interface Hit {
  libraryId: string
  title: string
  score: number
  outcome: string
  tags: string[]
  statutes: string[]
  excerpt: string
}

/** 单个 issue 的检索结果分组。 */
interface IssueGroup {
  issueId: string
  query: string
  mode: string
  relaxed: string[]
  totalCandidates: number
  message: string
  error: string
  hits: Hit[]
  selected: string[]
}

function readHits(raw: unknown): Hit[] {
  return pickArray(asRecord(raw), 'hits').map((item, i) => {
    const record = asRecord(item)
    const fm = asRecord(record?.frontmatter)
    return {
      libraryId: pickStr(record, 'library_id', 'id') || String(i + 1),
      title: pickStr(record, 'title') || pickStr(fm, 'title') || pickStr(record, 'case_id'),
      score: num(record?.score) ?? 0,
      outcome: pickStr(fm, 'outcome'),
      tags: strList(fm?.tags),
      statutes: strList(fm?.statutes),
      excerpt: pickStr(record, 'chunk_text', 'text', 'excerpt'),
    }
  })
}

function readGroups(defaults: Record<string, unknown>): IssueGroup[] {
  return pickArray(defaults, 'issues').map((raw, i) => {
    const record = asRecord(raw)
    const hits = readHits(raw)
    const selected = strList(record?.selected)
    return {
      issueId: pickStr(record, 'issue_id', 'id') || `I${i + 1}`,
      query: pickStr(record, 'query'),
      mode: pickStr(record, 'retrieval_mode'),
      relaxed: strList(record?.relaxed),
      totalCandidates: num(record?.total_candidates) ?? hits.length,
      message: pickStr(record, 'message'),
      error: pickStr(record, 'error'),
      hits,
      selected: selected.length > 0 ? selected : hits.slice(0, 2).map((h) => h.libraryId),
    }
  })
}

/**
 * §3.5 OaRetrieveCard（kind='oa_retrieve'，后端 P2 人审闸门）：
 * 顶部明示检索方式徽章（向量检索 / 暴力余弦 / 关键词匹配；无库时空态提示），
 * 逐 issue 列出相似案例卡（标题 + 相似度徽章 + 结案结果徽章 + 标签 chips + 勾选纳入），
 * 提交 `{selections:[{issue_id, library_ids}]}`（对齐 `pipelines/oa.py::retrieve`）。
 */
export function OaRetrieveCard({ stage, submit, skip, busy }: StageCardProps) {
  const payload = stage.payload as InteractionRequiredEvent | null
  const defaults = useMemo(() => readDefault(payload), [payload])
  const groups = useMemo(() => readGroups(defaults), [defaults])

  const [selections, setSelections] = useState<Record<string, string[]>>(() =>
    Object.fromEntries(groups.map((g) => [g.issueId, g.selected])),
  )

  const mode = pickStr(defaults, 'retrieval_mode') || groups[0]?.mode || 'keyword'
  const modeLabel = zh.oa.labels.retrievalModes[mode] ?? mode
  const totalHits = groups.reduce((sum, g) => sum + g.hits.length, 0)
  const totalSelected = Object.values(selections).reduce((sum, ids) => sum + ids.length, 0)

  const toggle = (issueId: string, libraryId: string): void => {
    setSelections((prev) => {
      const current = prev[issueId] ?? []
      return {
        ...prev,
        [issueId]: current.includes(libraryId)
          ? current.filter((x) => x !== libraryId)
          : [...current, libraryId],
      }
    })
  }

  const handleConfirm = (): void => {
    submit({
      selections: groups.map((g) => ({
        issue_id: g.issueId,
        library_ids: selections[g.issueId] ?? [],
      })),
    })
  }

  return (
    <StageCardShell
      stage={stage}
      icon={CircleStackIcon}
      title={zh.oa.retrieve.title}
      summary={zh.oa.retrieve.summary(totalSelected)}
      onConfirm={handleConfirm}
      onSkip={skip}
      confirmLabel={zh.oa.retrieve.confirm}
      busy={busy}
    >
      <div className="space-y-3">
        {/* 检索方式明示（backend-architecture：retrieval_mode 必须透出） */}
        <div className="flex flex-wrap items-center gap-2">
          <span className="inline-flex items-center gap-1.5 px-2 py-1 rounded-full bg-indigo-50 dark:bg-indigo-500/10 text-[11px] font-medium text-[#6366f1] dark:text-indigo-400">
            <MagnifyingGlassIcon className="w-3.5 h-3.5" strokeWidth={1.5} />
            {zh.oa.retrieve.modeLabel}：{modeLabel}
          </span>
          <span className="text-[11px] text-gray-500 dark:text-gray-400">
            {zh.oa.retrieve.hitCount(totalHits)} · {zh.oa.retrieve.selectedCount(totalSelected, totalHits)}
          </span>
        </div>

        {payload?.prompt && (
          <p className="text-sm text-gray-700 dark:text-gray-300 whitespace-pre-wrap">
            {payload.prompt}
          </p>
        )}

        {groups.length === 0 ? (
          <EmptyState
            icon={CircleStackIcon}
            title={zh.oa.retrieve.emptyTitle}
            description={zh.oa.retrieve.emptyDesc}
            className="py-6"
          />
        ) : (
          groups.map((group) => {
            const picked = selections[group.issueId] ?? []
            return (
              <div key={group.issueId} className="space-y-2">
                <div className="flex flex-wrap items-baseline gap-2">
                  <span className="text-[13px] font-medium text-gray-900 dark:text-gray-100">
                    {zh.oa.strategy.issueLabel(group.issueId)}
                  </span>
                  {group.query && (
                    <span className="text-[11px] text-gray-400 dark:text-gray-500 truncate">
                      {zh.oa.retrieve.queryLabel}：{group.query}
                    </span>
                  )}
                  {group.totalCandidates > 0 && (
                    <span className="text-[11px] text-gray-400 dark:text-gray-500">
                      {zh.oa.retrieve.candidates(group.totalCandidates)}
                    </span>
                  )}
                  {group.relaxed.length > 0 && (
                    <Badge variant="amber">
                      {zh.oa.retrieve.relaxed}：{group.relaxed.join('、')}
                    </Badge>
                  )}
                </div>

                {group.hits.length === 0 ? (
                  <div className="rounded-xl border border-dashed border-gray-200 dark:border-gray-700 px-3 py-4 text-center">
                    <p className="text-[13px] text-gray-500 dark:text-gray-400">
                      {zh.oa.retrieve.emptyTitle}
                    </p>
                    <p className="mt-0.5 text-[11px] text-gray-400 dark:text-gray-500">
                      {group.error || group.message || zh.oa.retrieve.emptyDesc}
                    </p>
                  </div>
                ) : (
                  <div className="space-y-2">
                    {group.hits.map((hit) => {
                      const active = picked.includes(hit.libraryId)
                      return (
                        <button
                          key={hit.libraryId}
                          type="button"
                          onClick={() => toggle(group.issueId, hit.libraryId)}
                          className={cn(
                            'w-full text-left rounded-xl border p-3 transition-colors',
                            active
                              ? 'border-[#6366f1] bg-indigo-50/40 dark:bg-indigo-500/10'
                              : 'border-gray-200 dark:border-gray-700 hover:border-indigo-300 dark:hover:border-indigo-500/40',
                          )}
                        >
                          <div className="flex items-start gap-2">
                            <span
                              className={cn(
                                'mt-0.5 w-4 h-4 shrink-0 rounded border flex items-center justify-center transition-colors',
                                active
                                  ? 'bg-[#6366f1] border-[#6366f1] text-white'
                                  : 'border-gray-300 dark:border-gray-600 text-transparent',
                              )}
                              aria-hidden="true"
                            >
                              <CheckIcon className="w-3 h-3" strokeWidth={2.5} />
                            </span>
                            <span className="flex-1 min-w-0 space-y-1.5">
                              <span className="flex flex-wrap items-center gap-1.5">
                                <span className="text-[13px] font-medium text-gray-900 dark:text-gray-100 truncate">
                                  {hit.title}
                                </span>
                                <Badge variant="indigo">
                                  {zh.oa.retrieve.score(scorePercent(hit.score))}
                                </Badge>
                                {hit.outcome && (
                                  <Badge variant={outcomeBadgeVariant(hit.outcome)}>
                                    {zh.oa.labels.outcomes[hit.outcome] ?? hit.outcome}
                                  </Badge>
                                )}
                                <span className="ml-auto text-[10px] text-gray-400 dark:text-gray-500">
                                  {active ? zh.oa.retrieve.included : zh.oa.retrieve.include}
                                </span>
                              </span>
                              {hit.excerpt && (
                                <span className="block text-[12px] leading-relaxed text-gray-500 dark:text-gray-400 line-clamp-2">
                                  {hit.excerpt}
                                </span>
                              )}
                              {(hit.tags.length > 0 || hit.statutes.length > 0) && (
                                <span className="flex flex-wrap gap-1">
                                  {[...hit.statutes, ...hit.tags].slice(0, 6).map((tag, i) => (
                                    <span
                                      key={`${tag}-${i}`}
                                      className="px-1.5 py-0.5 rounded-full bg-gray-100 dark:bg-gray-700 text-[10px] text-gray-500 dark:text-gray-300"
                                    >
                                      {str(tag)}
                                    </span>
                                  ))}
                                </span>
                              )}
                            </span>
                          </div>
                        </button>
                      )
                    })}
                  </div>
                )}
              </div>
            )
          })
        )}
      </div>
    </StageCardShell>
  )
}
