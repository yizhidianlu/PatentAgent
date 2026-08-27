import { QuestionMarkCircleIcon } from '@heroicons/react/24/outline'
import { zh } from '../../i18n/zh'
import { StageCardShell, type StageCardProps } from '../pipeline/StageCardShell'
import type { InteractionRequiredEvent } from '../../types/stream'

/** §4 未知 kind 兜底：prompt + 原始载荷 JSON + 确认/跳过。 */
export function UnknownStageCard({ stage, submit, skip, busy }: StageCardProps) {
  const payload = stage.payload as InteractionRequiredEvent | null

  return (
    <StageCardShell
      stage={stage}
      icon={QuestionMarkCircleIcon}
      summary={zh.stages.unknown.summary}
      onConfirm={() => submit({ confirmed: true })}
      onSkip={skip}
      busy={busy}
    >
      <div className="space-y-3">
        {payload?.prompt && (
          <p className="text-sm text-gray-700 dark:text-gray-300">{payload.prompt}</p>
        )}
        {payload?.schema != null && (
          <details className="text-xs text-gray-500 dark:text-gray-400">
            <summary className="cursor-pointer select-none">{zh.stage.schemaLabel}</summary>
            <pre className="mt-2 rounded-lg bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 p-3 overflow-x-auto scrollbar-thin">
              {JSON.stringify(payload.schema, null, 2)}
            </pre>
          </details>
        )}
      </div>
    </StageCardShell>
  )
}
