import { createElement, memo } from 'react'
import { ExclamationTriangleIcon, PaperClipIcon } from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { formatFileSize } from '../../lib/format'
import type { CaseSessionState } from '../../stores/sessionStore'
import type { StreamItem } from '../../types/stream'
import { StreamingMarkdown } from '../markdown/StreamingMarkdown'
import { DocumentCard } from '../document/DocumentCard'
import { Button } from '../ui/Button'
import { Spinner } from '../ui/Spinner'
import { resolveStageCard } from './stageCardRegistry'

export interface StreamItemViewProps {
  item: StreamItem
  caseId: string
  session: CaseSessionState
  /** 文档卡标题（缺省「生成文档」）。 */
  docTitle?: string
  /** stage 卡提交（父层负责乐观 completed + POST /pipeline/input）。 */
  onStageSubmit: (stageId: string, payload: unknown) => void
  onStageSkip: (stageId: string) => void
  /** 正在提交的 stage id。 */
  busyStageId?: string | null
  /** 失败卡「重试此步」（POST /pipeline/retry {step_key}）。 */
  onRetryStep?: (stepKey?: string) => void
  /** 重试请求进行中。 */
  retrying?: boolean
}

/**
 * §3.2 流条目分发渲染：
 * user 右对齐气泡（+附件 mini pills）/ assistant 全宽 markdown /
 * stage 卡（stageCardRegistry）/ doc_ref 流内 DocumentCard（xl 以下）/ notice 居中灰字。
 */
export const StreamItemView = memo(function StreamItemView({
  item,
  caseId,
  session,
  docTitle,
  onStageSubmit,
  onStageSkip,
  busyStageId,
  onRetryStep,
  retrying,
}: StreamItemViewProps) {
  switch (item.kind) {
    case 'user':
      return (
        <div className="flex flex-col items-end gap-1.5">
          <div className="self-end max-w-[85%] bg-gray-100 dark:bg-gray-800 rounded-2xl px-4 py-2.5 text-[15px] text-gray-900 dark:text-gray-100 whitespace-pre-wrap break-words">
            {item.text}
          </div>
          {item.files && item.files.length > 0 && (
            <div className="flex flex-wrap justify-end gap-1.5 max-w-[85%]">
              {item.files.map((file, i) => (
                <span
                  key={file.id ?? `${file.name}-${i}`}
                  className={cn(
                    'inline-flex items-center gap-1 rounded-lg px-2 py-1',
                    'bg-gray-50 dark:bg-gray-800/60 border border-gray-200 dark:border-gray-700',
                    'text-[11px] text-gray-600 dark:text-gray-300 max-w-[220px]',
                  )}
                >
                  <PaperClipIcon className="w-3 h-3 shrink-0" strokeWidth={1.5} />
                  <span className="truncate">{file.name}</span>
                  {typeof file.size === 'number' && (
                    <span className="shrink-0 text-gray-400">{formatFileSize(file.size)}</span>
                  )}
                </span>
              ))}
            </div>
          )}
        </div>
      )

    case 'assistant':
      return <StreamingMarkdown markdown={item.markdown} streaming={item.streaming} />

    case 'stage':
      // 注册表分发：组件取自模块级常量表，createElement 避免「渲染期创建组件」误报
      return createElement(resolveStageCard(item.stage.type), {
        caseId,
        stage: item.stage,
        submit: (payload: unknown) => onStageSubmit(item.stage.id, payload),
        skip: () => onStageSkip(item.stage.id),
        busy: busyStageId === item.stage.id,
      })

    case 'doc_ref': {
      const doc = session.docs[item.docId]
      if (!doc) return null
      // xl 及以上由右侧 DocumentPanel 承载，流内卡片仅在窄屏出现
      return (
        <div className="xl:hidden">
          <DocumentCard title={docTitle ?? zh.document.defaultDocTitle} doc={doc} />
        </div>
      )
    }

    case 'notice': {
      if (item.level !== 'error') {
        return (
          <p className="text-center text-xs text-gray-400 dark:text-gray-500">{item.text}</p>
        )
      }
      // 流水线步骤失败卡（§2 错误态）：红卡 + 步骤名 + 原因 + 可重试时的「重试此步」
      return (
        <div
          role="alert"
          className={cn(
            'rounded-2xl border px-4 py-3 flex items-start gap-2.5',
            'border-red-200 dark:border-red-500/30 bg-red-50 dark:bg-red-500/10',
          )}
        >
          <ExclamationTriangleIcon
            className="w-5 h-5 shrink-0 mt-px text-red-500 dark:text-red-400"
            strokeWidth={1.5}
          />
          <div className="min-w-0 flex-1 space-y-1">
            <p className="text-sm font-medium text-red-700 dark:text-red-300">
              {item.stepLabel ? zh.pipeline.stepFailed(item.stepLabel) : zh.pipeline.runFailed}
            </p>
            <p className="text-xs leading-relaxed text-red-600/90 dark:text-red-400/90 break-words whitespace-pre-wrap">
              {item.text}
            </p>
            {!item.retryable && (
              <p className="text-[11px] text-red-500/80 dark:text-red-400/70">
                {zh.pipeline.notRetryableHint}
              </p>
            )}
          </div>
          {item.retryable && onRetryStep && (
            <Button
              variant="secondary"
              size="sm"
              disabled={retrying}
              onClick={() => onRetryStep(item.stepKey)}
              className="shrink-0 gap-1.5"
            >
              {retrying && <Spinner size="sm" />}
              {zh.pipeline.retryStep}
            </Button>
          )}
        </div>
      )
    }
  }
})
