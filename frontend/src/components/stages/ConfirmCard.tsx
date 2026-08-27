import { CheckCircleIcon } from '@heroicons/react/24/outline'
import { zh } from '../../i18n/zh'
import { StageCardShell, type StageCardProps } from '../pipeline/StageCardShell'
import type { InteractionRequiredEvent } from '../../types/stream'

/** §4 通用确认卡：prompt + 确认/跳过（preview_confirm / self_check 等亦可复用）。 */
export function ConfirmCard({ stage, submit, skip, busy }: StageCardProps) {
  const payload = stage.payload as InteractionRequiredEvent | null

  return (
    <StageCardShell
      stage={stage}
      icon={CheckCircleIcon}
      summary={zh.stages.confirmCard.summary}
      onConfirm={() => submit({ confirmed: true })}
      onSkip={skip}
      busy={busy}
    >
      {payload?.prompt ? (
        <p className="text-sm text-gray-700 dark:text-gray-300">{payload.prompt}</p>
      ) : null}
    </StageCardShell>
  )
}
