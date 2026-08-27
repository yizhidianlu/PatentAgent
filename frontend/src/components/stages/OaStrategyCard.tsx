import { useMemo, useState } from 'react'
import { LightBulbIcon } from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { Badge } from '../ui/Badge'
import { StageCardShell, type StageCardProps } from '../pipeline/StageCardShell'
import { asRecord, pickArray, pickStr, readDefault } from './stagePayload'
import type { InteractionRequiredEvent } from '../../types/stream'

/** D3 答复策略枚举（models/oa.py::RESPONSE_STRATEGIES，顺序即 tile 顺序）。 */
const STRATEGIES = ['argue_only', 'amend_claims', 'amend_description', 'formal_fix'] as const
type StrategyKey = (typeof STRATEGIES)[number]

interface StrategyOption {
  strategy: string
  rationale: string
  risk: string
  sketch: string
}

interface IssuePlan {
  issueId: string
  options: StrategyOption[]
  recommended: string
}

interface Choice {
  strategy: string
  note: string
}

function readPlans(defaults: Record<string, unknown>): IssuePlan[] {
  return pickArray(defaults, 'strategies', 'plans').map((raw, i) => {
    const record = asRecord(raw)
    const options = pickArray(record, 'options').map((item) => {
      const option = asRecord(item)
      return {
        strategy: pickStr(option, 'strategy'),
        rationale: pickStr(option, 'rationale'),
        risk: pickStr(option, 'risk'),
        sketch: pickStr(option, 'amendment_sketch', 'sketch'),
      }
    })
    return {
      issueId: pickStr(record, 'issue_id', 'id') || `I${i + 1}`,
      options,
      recommended: pickStr(record, 'recommended') || options[0]?.strategy || 'argue_only',
    }
  })
}

/**
 * §3.5 OaStrategyCard（kind='oa_strategy'，后端 P3 人审闸门）：
 * 逐 issue 四张策略 tile（仅意见陈述 / 修改权利要求 / 修改说明书 / 补正形式），
 * tile 内含 rationale 与 risk 小字、recommended 高亮「推荐」徽章；
 * 每条一个备注 textarea（带入起草）；底部主行动为「生成答复草稿」。
 * 提交 `{choices:[{issue_id, strategy, note}]}`（对齐 `pipelines/oa.py::strategy`）。
 */
export function OaStrategyCard({ stage, submit, skip, busy }: StageCardProps) {
  const payload = stage.payload as InteractionRequiredEvent | null
  const defaults = useMemo(() => readDefault(payload), [payload])
  const plans = useMemo(() => readPlans(defaults), [defaults])

  /** 后端 default.choices 为逐条初值（缺省取 recommended）。 */
  const initial = useMemo<Record<string, Choice>>(() => {
    const out: Record<string, Choice> = {}
    for (const plan of plans) out[plan.issueId] = { strategy: plan.recommended, note: '' }
    for (const raw of pickArray(defaults, 'choices')) {
      const record = asRecord(raw)
      const issueId = pickStr(record, 'issue_id', 'id')
      if (!issueId) continue
      out[issueId] = {
        strategy: pickStr(record, 'strategy') || out[issueId]?.strategy || 'argue_only',
        note: pickStr(record, 'note'),
      }
    }
    return out
  }, [defaults, plans])

  const [choices, setChoices] = useState<Record<string, Choice>>(initial)

  const issueIds = useMemo(() => {
    const ids = plans.map((p) => p.issueId)
    for (const id of Object.keys(initial)) if (!ids.includes(id)) ids.push(id)
    return ids
  }, [plans, initial])

  const pick = (issueId: string, strategy: StrategyKey): void => {
    setChoices((prev) => ({
      ...prev,
      [issueId]: { strategy, note: prev[issueId]?.note ?? '' },
    }))
  }

  const setNote = (issueId: string, note: string): void => {
    setChoices((prev) => ({
      ...prev,
      [issueId]: { strategy: prev[issueId]?.strategy ?? 'argue_only', note },
    }))
  }

  const handleConfirm = (): void => {
    submit({
      choices: issueIds.map((issueId) => ({
        issue_id: issueId,
        strategy: choices[issueId]?.strategy ?? 'argue_only',
        note: choices[issueId]?.note ?? '',
      })),
    })
  }

  return (
    <StageCardShell
      stage={stage}
      icon={LightBulbIcon}
      title={zh.oa.strategy.title}
      summary={zh.oa.strategy.summary(issueIds.length)}
      onConfirm={handleConfirm}
      onSkip={skip}
      confirmLabel={zh.oa.strategy.confirm}
      busy={busy}
    >
      <div className="space-y-4">
        {payload?.prompt && (
          <p className="text-sm text-gray-700 dark:text-gray-300 whitespace-pre-wrap">
            {payload.prompt}
          </p>
        )}

        {issueIds.length === 0 ? (
          <p className="text-sm text-gray-400 dark:text-gray-500">{zh.oa.strategy.empty}</p>
        ) : (
          issueIds.map((issueId) => {
            const plan = plans.find((p) => p.issueId === issueId)
            const choice = choices[issueId]
            const byStrategy = new Map(plan?.options.map((o) => [o.strategy, o]) ?? [])
            return (
              <div key={issueId} className="space-y-2">
                <div className="flex items-center gap-2">
                  <span className="text-[13px] font-medium text-gray-900 dark:text-gray-100">
                    {zh.oa.strategy.issueLabel(issueId)}
                  </span>
                </div>

                <div className="grid sm:grid-cols-2 gap-2">
                  {STRATEGIES.map((key) => {
                    const option = byStrategy.get(key)
                    const active = (choice?.strategy ?? '') === key
                    const recommended = plan?.recommended === key
                    return (
                      <button
                        key={key}
                        type="button"
                        onClick={() => pick(issueId, key)}
                        className={cn(
                          'text-left rounded-xl border-2 p-3 transition-colors',
                          active
                            ? 'border-[#6366f1] bg-indigo-50/50 dark:bg-indigo-500/10'
                            : recommended
                              ? 'border-indigo-200 dark:border-indigo-500/40 hover:border-[#6366f1]'
                              : 'border-gray-200 dark:border-gray-700 hover:border-indigo-300 dark:hover:border-indigo-500/40',
                        )}
                      >
                        <span className="flex items-center gap-1.5">
                          <span className="text-[13px] font-medium text-gray-900 dark:text-gray-100">
                            {zh.oa.labels.strategies[key]}
                          </span>
                          {recommended && (
                            <Badge variant="indigo">{zh.oa.strategy.recommended}</Badge>
                          )}
                        </span>
                        {option ? (
                          <span className="mt-1 block space-y-1">
                            {option.rationale && (
                              <span className="block text-[11px] leading-relaxed text-gray-500 dark:text-gray-400">
                                {zh.oa.strategy.rationale}：{option.rationale}
                              </span>
                            )}
                            {option.risk && (
                              <span className="block text-[11px] leading-relaxed text-amber-600 dark:text-amber-400">
                                {zh.oa.strategy.risk}：{option.risk}
                              </span>
                            )}
                            {option.sketch && (
                              <span className="block text-[11px] leading-relaxed text-gray-400 dark:text-gray-500">
                                {zh.oa.strategy.sketch}：{option.sketch}
                              </span>
                            )}
                          </span>
                        ) : (
                          <span className="mt-1 block text-[11px] text-gray-400 dark:text-gray-500">
                            {zh.oa.strategy.notOffered}
                          </span>
                        )}
                      </button>
                    )
                  })}
                </div>

                <label className="block space-y-1">
                  <span className="block text-[11px] text-gray-500 dark:text-gray-400">
                    {zh.oa.strategy.noteLabel}
                  </span>
                  <textarea
                    value={choice?.note ?? ''}
                    rows={2}
                    placeholder={zh.oa.strategy.notePlaceholder}
                    onChange={(e) => setNote(issueId, e.target.value)}
                    className="w-full rounded-xl border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 px-3 py-2 text-[13px] text-gray-900 dark:text-gray-100 placeholder:text-gray-400 outline-none focus:border-[#6366f1] focus:ring-4 focus:ring-indigo-500/10 resize-y"
                  />
                </label>
              </div>
            )
          })
        )}
      </div>
    </StageCardShell>
  )
}
