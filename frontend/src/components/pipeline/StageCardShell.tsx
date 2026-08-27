import { useState, type ComponentType, type ReactNode, type SVGProps } from 'react'
import { CheckCircleIcon, ChevronDownIcon, SparklesIcon } from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { Badge } from '../ui/Badge'
import { Button } from '../ui/Button'
import { Card } from '../ui/Card'
import { Spinner } from '../ui/Spinner'
import type { Stage } from '../../types/stream'

/** 各 StageCard 的统一 props（stageCardRegistry 分发）。 */
export interface StageCardProps {
  caseId: string
  stage: Stage
  /** 提交（父层乐观 completed + POST /pipeline/input {step_key,payload}）。 */
  submit: (payload: unknown) => void
  /** 跳过。 */
  skip: () => void
  /** 提交请求进行中。 */
  busy?: boolean
}

export interface StageCardShellProps {
  stage: Stage
  /** 头部标题（缺省按 kind 从 zh.stage.kinds 取）。 */
  title?: string
  icon?: ComponentType<SVGProps<SVGSVGElement>>
  /** completed 折叠行的一行摘要。 */
  summary?: string
  /** 体内容。 */
  children?: ReactNode
  /** 自定义底栏（缺省渲染 跳过/确认）。 */
  footer?: ReactNode
  onConfirm?: () => void
  onSkip?: () => void
  confirmLabel?: string
  skipLabel?: string
  confirmDisabled?: boolean
  /** 提交中（确认钮 Spinner）。 */
  busy?: boolean
  /** 隐藏跳过钮。 */
  hideSkip?: boolean
  className?: string
}

/**
 * §4 StageCardShell：active（indigo 描边 + 头条 + 体 + 底栏）/
 * completed（折叠一行摘要，可重展只读）/ skipped（灰行）三态外壳。
 */
export function StageCardShell({
  stage,
  title,
  icon: Icon = SparklesIcon,
  summary,
  children,
  footer,
  onConfirm,
  onSkip,
  confirmLabel = zh.stage.confirm,
  skipLabel = zh.stage.skip,
  confirmDisabled = false,
  busy = false,
  hideSkip = false,
  className,
}: StageCardShellProps) {
  const [reExpanded, setReExpanded] = useState(false)
  const label = title ?? zh.stage.kinds[stage.type] ?? zh.stage.unknownKind

  // completed / skipped：折叠一行摘要
  if (stage.status !== 'active') {
    const skipped = stage.status === 'skipped'
    const line = skipped
      ? zh.stage.skippedBadge
      : (summary ?? zh.stage.completedBadge)
    return (
      <Card className={cn('overflow-hidden', className)}>
        <button
          type="button"
          onClick={() => !skipped && children && setReExpanded((v) => !v)}
          className={cn(
            'w-full px-4 py-2.5 flex items-center gap-2 text-left',
            !skipped && children && 'hover:bg-gray-50 dark:hover:bg-gray-700/40 transition-colors',
          )}
        >
          <CheckCircleIcon
            className={cn('w-4 h-4 shrink-0', skipped ? 'text-gray-400' : 'text-emerald-500')}
            strokeWidth={1.5}
          />
          <span
            className={cn(
              'text-[13px] truncate flex-1 min-w-0',
              skipped ? 'text-gray-400' : 'text-gray-600 dark:text-gray-300',
            )}
          >
            <span className="font-medium">{label}</span>
            {' · '}
            {line}
          </span>
          {!skipped && children && (
            <ChevronDownIcon
              className={cn(
                'w-4 h-4 shrink-0 text-gray-400 transition-transform duration-200',
                reExpanded && 'rotate-180',
              )}
              strokeWidth={1.5}
              aria-label={reExpanded ? zh.stage.collapse : zh.stage.reExpand}
            />
          )}
        </button>
        {reExpanded && !skipped && children && (
          <fieldset
            disabled
            className="p-4 border-t border-gray-100 dark:border-gray-700 opacity-80 pointer-events-none"
          >
            {children}
          </fieldset>
        )}
      </Card>
    )
  }

  // active
  return (
    <Card
      className={cn(
        'border-2 border-indigo-200 dark:border-indigo-500/40 overflow-hidden',
        className,
      )}
    >
      <div className="px-4 py-2.5 bg-indigo-50/50 dark:bg-indigo-500/10 flex items-center gap-2">
        <span className="w-6 h-6 rounded-lg bg-[#6366f1] text-white flex items-center justify-center shrink-0">
          <Icon className="w-3.5 h-3.5" strokeWidth={1.5} />
        </span>
        <span className="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">
          {label}
        </span>
        <Badge variant="amber" className="animate-pulse ml-auto shrink-0">
          {zh.stage.pendingBadge}
        </Badge>
      </div>

      {children && <div className="p-4">{children}</div>}

      {footer !== undefined ? (
        footer
      ) : (
        <div className="flex justify-end gap-2 px-4 py-3 bg-gray-50/50 dark:bg-gray-800/60">
          {!hideSkip && onSkip && (
            <Button variant="ghost" size="sm" onClick={onSkip} disabled={busy}>
              {skipLabel}
            </Button>
          )}
          {onConfirm && (
            <Button size="sm" onClick={onConfirm} disabled={confirmDisabled || busy} className="gap-1.5">
              {busy && <Spinner size="sm" className="border-white/40 border-t-white" />}
              {confirmLabel}
            </Button>
          )}
        </div>
      )}
    </Card>
  )
}
