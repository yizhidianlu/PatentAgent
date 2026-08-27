import { ArrowPathRoundedSquareIcon } from '@heroicons/react/24/outline'
import { zh } from '../../i18n/zh'
import { Button } from '../ui/Button'
import { StageCardShell, type StageCardProps } from '../pipeline/StageCardShell'
import type { InteractionRequiredEvent } from '../../types/stream'
import { asRecord, pickStr, readDefault } from './stagePayload'

function typeLabel(value: string): string {
  return zh.stages.intake.types[value] ?? value
}

/**
 * §2.8 模块 A / A2b 类型改判建议卡（intake.md 反问模板）：
 * 提示文案 + 两按钮「改为实用新型 / 外观设计」·「保持发明」。
 * 提交 `{patent_type, switched}`。
 */
export function TypeSuggestCard({ stage, submit, skip, busy }: StageCardProps) {
  const payload = stage.payload as InteractionRequiredEvent | null
  const defaults = readDefault(payload)

  const suggested =
    pickStr(defaults, 'suggested_type', 'suggestion', 'target_type', 'patent_type') ||
    'utility_model'
  const current = pickStr(defaults, 'current_type', 'from_type') || 'invention'
  const reason = pickStr(defaults, 'reason', 'rationale', 'evidence')

  const choice = pickStr(asRecord(stage.result), 'patent_type')

  return (
    <StageCardShell
      stage={stage}
      icon={ArrowPathRoundedSquareIcon}
      title={zh.stage.kinds.type_suggest}
      summary={zh.stages.typeSuggest.summary(typeLabel(choice || current))}
      onSkip={skip}
      busy={busy}
      footer={
        <div className="flex flex-wrap justify-end gap-2 px-4 py-3 bg-gray-50/50 dark:bg-gray-800/60">
          <Button
            variant="ghost"
            size="sm"
            disabled={busy}
            onClick={() => submit({ patent_type: current, switched: false })}
          >
            {zh.stages.typeSuggest.keep(typeLabel(current))}
          </Button>
          <Button
            size="sm"
            disabled={busy}
            onClick={() => submit({ patent_type: suggested, switched: true })}
          >
            {zh.stages.typeSuggest.switchTo(typeLabel(suggested))}
          </Button>
        </div>
      }
    >
      <div className="space-y-2">
        <p className="text-sm leading-relaxed text-gray-700 dark:text-gray-300 whitespace-pre-wrap">
          {payload?.prompt || zh.stages.typeSuggest.defaultPrompt}
        </p>
        {reason && (
          <p className="text-xs leading-relaxed text-gray-500 dark:text-gray-400">{reason}</p>
        )}
      </div>
    </StageCardShell>
  )
}
