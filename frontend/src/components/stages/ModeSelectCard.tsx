import { useState } from 'react'
import { BoltIcon, HandRaisedIcon, AdjustmentsHorizontalIcon } from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { StageCardShell, type StageCardProps } from '../pipeline/StageCardShell'
import type { InteractionRequiredEvent } from '../../types/stream'

interface ModeOption {
  value: string
  title: string
  desc: string
  Icon: typeof BoltIcon
}

/** direct / human-in-loop 已知模式的标签映射；未知值回退通用 tile。 */
function buildOptions(enumValues: string[]): ModeOption[] {
  return enumValues.map((value) => {
    if (value === 'direct') {
      return {
        value,
        title: zh.stages.modeSelect.directTitle,
        desc: zh.stages.modeSelect.directDesc,
        Icon: BoltIcon,
      }
    }
    if (value === 'human_in_loop' || value === 'confirm_each' || value === 'hil') {
      return {
        value,
        title: zh.stages.modeSelect.hilTitle,
        desc: zh.stages.modeSelect.hilDesc,
        Icon: HandRaisedIcon,
      }
    }
    return { value, title: value, desc: '', Icon: AdjustmentsHorizontalIcon }
  })
}

function readEnum(schema: unknown): string[] {
  const props = (schema as { properties?: { mode?: { enum?: unknown[] } } } | null)?.properties
  const values = props?.mode?.enum
  if (Array.isArray(values) && values.every((v) => typeof v === 'string') && values.length > 0) {
    return values as string[]
  }
  return ['direct', 'human_in_loop']
}

/** §3.3 ModeSelectCard：direct / human-in-loop 两 tile，橙 accent。 */
export function ModeSelectCard({ stage, submit, skip, busy }: StageCardProps) {
  const payload = stage.payload as InteractionRequiredEvent | null
  const options = buildOptions(readEnum(payload?.schema))
  const resultMode = (stage.result as { mode?: string } | undefined)?.mode
  const [selected, setSelected] = useState<string | null>(resultMode ?? null)

  const selectedOption = options.find((o) => o.value === selected)
  const summaryLabel =
    options.find((o) => o.value === resultMode)?.title ?? resultMode ?? selectedOption?.title ?? ''

  return (
    <StageCardShell
      stage={stage}
      icon={AdjustmentsHorizontalIcon}
      summary={summaryLabel ? zh.stages.modeSelect.summary(summaryLabel) : undefined}
      onConfirm={() => selected && submit({ mode: selected })}
      onSkip={skip}
      confirmLabel={zh.stages.modeSelect.confirm}
      confirmDisabled={!selected}
      busy={busy}
    >
      <div className="space-y-3">
        {payload?.prompt && (
          <p className="text-sm text-gray-700 dark:text-gray-300">{payload.prompt}</p>
        )}
        <div className="grid sm:grid-cols-2 gap-3">
          {options.map(({ value, title, desc, Icon }) => {
            const active = selected === value
            return (
              <button
                key={value}
                type="button"
                onClick={() => setSelected(value)}
                className={cn(
                  'rounded-xl border-2 p-4 text-left transition-colors outline-none',
                  'focus-visible:ring-4 focus-visible:ring-orange-500/10',
                  active
                    ? 'border-orange-500 bg-orange-50/50 dark:bg-orange-500/10'
                    : 'border-gray-200 dark:border-gray-700 hover:border-orange-400/50',
                )}
              >
                <span
                  className={cn(
                    'w-8 h-8 rounded-lg flex items-center justify-center mb-2',
                    active
                      ? 'bg-orange-500 text-white'
                      : 'bg-gray-100 dark:bg-gray-700 text-gray-500 dark:text-gray-300',
                  )}
                >
                  <Icon className="w-4 h-4" strokeWidth={1.5} />
                </span>
                <span className="block text-sm font-medium text-gray-900 dark:text-gray-100">
                  {title}
                </span>
                {desc && (
                  <span className="block text-xs text-gray-500 dark:text-gray-400 mt-1">
                    {desc}
                  </span>
                )}
              </button>
            )
          })}
        </div>
      </div>
    </StageCardShell>
  )
}
