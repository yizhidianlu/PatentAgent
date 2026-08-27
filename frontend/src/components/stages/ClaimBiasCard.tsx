import { useMemo, useState } from 'react'
import { ScaleIcon } from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { Button } from '../ui/Button'
import { StageCardShell, type StageCardProps } from '../pipeline/StageCardShell'
import type { InteractionRequiredEvent } from '../../types/stream'
import { asRecord, pickArray, pickStr, readDefault, str } from './stagePayload'

/** 单侧偏向选项（invention/claim_bias.md：{label, basis_quote}）。 */
interface BiasOption {
  key: 'a' | 'b'
  label: string
  basisQuote: string
}

interface BiasGroup {
  id: string
  axis: string
  options: BiasOption[]
}

function readOption(raw: unknown, key: 'a' | 'b'): BiasOption {
  const record = asRecord(raw)
  return {
    key,
    label: pickStr(record, 'label', 'title', 'text') || str(raw),
    basisQuote: pickStr(record, 'basis_quote', 'basis', 'quote'),
  }
}

function readGroups(payload: InteractionRequiredEvent | null): BiasGroup[] {
  const d = readDefault(payload)
  const raw = pickArray(d, 'groups', 'axes', 'items')
  return raw
    .map((item, i) => {
      const record = asRecord(item)
      const options = [
        readOption(record?.option_a ?? record?.a, 'a'),
        readOption(record?.option_b ?? record?.b, 'b'),
      ].filter((o) => o.label.trim().length > 0)
      return {
        id: pickStr(record, 'id', 'key', 'axis') || String(i + 1),
        axis: pickStr(record, 'axis', 'title', 'name') || `#${i + 1}`,
        options,
      }
    })
    .filter((g) => g.options.length > 0)
}

/**
 * §2.8 模块 A / A8 权利要求偏向卡：
 * 每组 axis 标题 + option_a / option_b 两块对举 tile（含 basis_quote 终稿引文小字），
 * 「确认偏向」提交所选；「保持现状」不作选择。选择仅存记录，不入正文。
 */
export function ClaimBiasCard({ stage, submit, skip, busy }: StageCardProps) {
  const payload = stage.payload as InteractionRequiredEvent | null
  const groups = useMemo(() => readGroups(payload), [payload])
  const [choices, setChoices] = useState<Record<string, 'a' | 'b'>>({})

  const chosenCount = Object.keys(choices).length

  const buildPayload = (keepCurrent: boolean) => ({
    keep_current: keepCurrent,
    choices: keepCurrent
      ? []
      : groups
          .filter((g) => choices[g.id])
          .map((g) => {
            const key = choices[g.id]
            const option = g.options.find((o) => o.key === key)
            return { axis: g.axis, choice: key, label: option?.label ?? '' }
          }),
  })

  return (
    <StageCardShell
      stage={stage}
      icon={ScaleIcon}
      title={zh.stage.kinds.claim_bias}
      summary={zh.stages.claimBias.summary(chosenCount)}
      onSkip={skip}
      busy={busy}
      footer={
        <div className="flex flex-wrap justify-end gap-2 px-4 py-3 bg-gray-50/50 dark:bg-gray-800/60">
          <Button
            variant="ghost"
            size="sm"
            disabled={busy}
            onClick={() => submit(buildPayload(true))}
          >
            {zh.stages.claimBias.keep}
          </Button>
          <Button
            size="sm"
            disabled={busy || chosenCount === 0}
            onClick={() => submit(buildPayload(false))}
          >
            {zh.stages.claimBias.confirm}
          </Button>
        </div>
      }
    >
      <div className="space-y-3">
        {payload?.prompt && (
          <p className="text-sm text-gray-700 dark:text-gray-300">{payload.prompt}</p>
        )}

        {groups.length === 0 ? (
          <p className="text-sm text-gray-400 dark:text-gray-500">{zh.stages.claimBias.empty}</p>
        ) : (
          groups.map((group) => (
            <section key={group.id} className="space-y-1.5">
              <h4 className="text-xs font-semibold text-gray-500 dark:text-gray-400">
                {group.axis}
              </h4>
              <div className="grid sm:grid-cols-2 gap-2">
                {group.options.map((option) => {
                  const active = choices[group.id] === option.key
                  return (
                    <button
                      key={option.key}
                      type="button"
                      onClick={() =>
                        setChoices((prev) =>
                          prev[group.id] === option.key
                            ? (() => {
                                const next = { ...prev }
                                delete next[group.id]
                                return next
                              })()
                            : { ...prev, [group.id]: option.key },
                        )
                      }
                      className={cn(
                        'rounded-xl border-2 p-3 text-left transition-colors outline-none',
                        'focus-visible:ring-4 focus-visible:ring-indigo-500/10',
                        active
                          ? 'border-[#6366f1] bg-indigo-50/50 dark:bg-indigo-500/10'
                          : 'border-gray-200 dark:border-gray-700 hover:border-indigo-400/50',
                      )}
                    >
                      <span className="block text-sm text-gray-900 dark:text-gray-100 leading-relaxed">
                        {option.label}
                      </span>
                      {option.basisQuote && (
                        <span className="block mt-1.5 text-[11px] leading-relaxed text-gray-400 dark:text-gray-500 border-l-2 border-gray-200 dark:border-gray-600 pl-2">
                          {zh.stages.claimBias.basisLabel}：{option.basisQuote}
                        </span>
                      )}
                    </button>
                  )
                })}
              </div>
            </section>
          ))
        )}

        <p className="text-[11px] text-gray-400 dark:text-gray-500">{zh.stages.claimBias.note}</p>
      </div>
    </StageCardShell>
  )
}
