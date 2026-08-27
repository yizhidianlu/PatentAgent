import type { ReactNode } from 'react'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import type { CaseSessionState } from '../../stores/sessionStore'
import { StreamItemView } from './StreamItemView'

export interface PipelineStreamProps {
  caseId: string
  session: CaseSessionState | undefined
  /** 流内 DocumentCard 标题。 */
  docTitle?: string
  /** 空会话态（mini-hero）。 */
  emptyState?: ReactNode
  onStageSubmit: (stageId: string, payload: unknown) => void
  onStageSkip: (stageId: string) => void
  busyStageId?: string | null
  /** 流内失败卡「重试此步」。 */
  onRetryStep?: (stepKey?: string) => void
  retrying?: boolean
  /**
   * 流末尾是否附「内容由 AI 生成」免责声明。
   * 工作台传 false —— chat Composer 底部已有同一句（§2.3），否则一屏出现两遍。
   */
  showDisclaimer?: boolean
  className?: string
}

/**
 * §3.2 会话流：`max-w-[800px] mx-auto px-4 py-6 space-y-4`，
 * 按 StreamItem 类型分发到 StreamItemView；空流渲染 emptyState。
 */
export function PipelineStream({
  caseId,
  session,
  docTitle,
  emptyState,
  onStageSubmit,
  onStageSkip,
  busyStageId,
  onRetryStep,
  retrying,
  showDisclaimer = true,
  className,
}: PipelineStreamProps) {
  const items = session?.items ?? []

  return (
    <div className={cn('max-w-[800px] mx-auto px-4 py-6 space-y-4', className)}>
      {items.length === 0
        ? emptyState
        : items.map((item) => (
            <StreamItemView
              key={item.id}
              item={item}
              caseId={caseId}
              session={session!}
              docTitle={docTitle}
              onStageSubmit={onStageSubmit}
              onStageSkip={onStageSkip}
              busyStageId={busyStageId}
              onRetryStep={onRetryStep}
              retrying={retrying}
            />
          ))}
      {showDisclaimer && items.length > 0 && (
        <p className="text-center text-xs text-gray-400 dark:text-gray-500 pt-2">
          {zh.common.aiDisclaimer}
        </p>
      )}
    </div>
  )
}
