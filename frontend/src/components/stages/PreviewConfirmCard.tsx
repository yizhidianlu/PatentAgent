import { useState } from 'react'
import { EyeIcon } from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { Button } from '../ui/Button'
import { Spinner } from '../ui/Spinner'
import { StageCardShell, type StageCardProps } from '../pipeline/StageCardShell'
import type { InteractionRequiredEvent } from '../../types/stream'
import { pickArray, pickStr, readDefault, str } from './stagePayload'

/** 五项摘要（preview.md：名称 / 类型 / 解决的技术问题 / 核心模块 / 与最接近现有技术的区别）。 */
interface PreviewRow {
  key: string
  label: string
  value: string
}

function readRows(payload: InteractionRequiredEvent | null): PreviewRow[] {
  const d = readDefault(payload)
  const fields = zh.stages.previewConfirm.fields
  const typeRaw = pickStr(d, 'patent_type', 'type')
  const modules = pickArray(d, 'modules', 'core_modules', 'components')
    .map(str)
    .filter(Boolean)
  return [
    { key: 'name', label: fields.name, value: pickStr(d, 'name', 'case_title', 'title') },
    {
      key: 'patent_type',
      label: fields.patent_type,
      value: zh.stages.intake.types[typeRaw] ?? typeRaw,
    },
    {
      key: 'problem',
      label: fields.problem,
      value: pickStr(d, 'problem', 'tech_problem', 'technical_problem'),
    },
    {
      key: 'modules',
      label: fields.modules,
      value: modules.length > 0 ? modules.join('、') : pickStr(d, 'modules_text'),
    },
    {
      key: 'distinction',
      label: fields.distinction,
      value: pickStr(d, 'distinction', 'distinctions', 'difference', 'closest_prior_art_diff'),
    },
  ]
}

const textareaCls =
  'w-full min-h-[88px] rounded-xl border border-gray-300 dark:border-gray-600 ' +
  'bg-white dark:bg-gray-800 px-3.5 py-2.5 text-sm text-gray-900 dark:text-gray-100 ' +
  'placeholder:text-gray-400 outline-none transition-colors scrollbar-thin ' +
  'focus:border-[#6366f1] focus:ring-4 focus:ring-indigo-500/10'

/**
 * §2.8 模块 A / A5 摘要预览卡：
 * dl 五项摘要 + 「调整方向」内联 textarea（带反馈重跑）+ 「确认生成」gradient 钮。
 */
export function PreviewConfirmCard({ stage, submit, skip, busy }: StageCardProps) {
  const payload = stage.payload as InteractionRequiredEvent | null
  const rows = readRows(payload)
  const [adjusting, setAdjusting] = useState(false)
  const [feedback, setFeedback] = useState('')

  return (
    <StageCardShell
      stage={stage}
      icon={EyeIcon}
      title={zh.stage.kinds.preview_confirm}
      summary={zh.stages.previewConfirm.summary}
      onSkip={skip}
      busy={busy}
      footer={
        <div className="flex flex-wrap items-center justify-end gap-2 px-4 py-3 bg-gray-50/50 dark:bg-gray-800/60">
          <Button variant="ghost" size="sm" disabled={busy} onClick={skip}>
            {zh.stage.skip}
          </Button>
          <Button
            variant="secondary"
            size="sm"
            disabled={busy}
            onClick={() => setAdjusting((v) => !v)}
          >
            {adjusting ? zh.stages.previewConfirm.adjustCancel : zh.stages.previewConfirm.adjust}
          </Button>
          {adjusting ? (
            <Button
              size="sm"
              disabled={busy || feedback.trim().length === 0}
              className="gap-1.5"
              onClick={() => submit({ confirmed: false, feedback: feedback.trim() })}
            >
              {busy && <Spinner size="sm" className="border-white/40 border-t-white" />}
              {zh.stages.previewConfirm.adjustSubmit}
            </Button>
          ) : (
            <Button
              variant="gradient"
              size="sm"
              disabled={busy}
              className="gap-1.5"
              onClick={() => submit({ confirmed: true })}
            >
              {busy && <Spinner size="sm" className="border-white/40 border-t-white" />}
              {zh.stages.previewConfirm.confirm}
            </Button>
          )}
        </div>
      }
    >
      <div className="space-y-3">
        {payload?.prompt && (
          <p className="text-sm text-gray-700 dark:text-gray-300">{payload.prompt}</p>
        )}

        <dl className="divide-y divide-gray-100 dark:divide-gray-700/60 rounded-xl border border-gray-200 dark:border-gray-700 overflow-hidden">
          {rows.map((row) => (
            <div key={row.key} className="grid sm:grid-cols-[9rem_1fr] gap-1 sm:gap-3 px-3 py-2.5">
              <dt className="text-xs font-medium text-gray-500 dark:text-gray-400">{row.label}</dt>
              <dd
                className={cn(
                  'text-sm leading-relaxed whitespace-pre-wrap break-words',
                  row.value
                    ? 'text-gray-800 dark:text-gray-200'
                    : 'text-gray-400 dark:text-gray-500',
                )}
              >
                {row.value || zh.stages.previewConfirm.empty}
              </dd>
            </div>
          ))}
        </dl>

        {adjusting && (
          <textarea
            className={textareaCls}
            value={feedback}
            autoFocus
            placeholder={zh.stages.previewConfirm.adjustPlaceholder}
            onChange={(e) => setFeedback(e.target.value)}
          />
        )}
      </div>
    </StageCardShell>
  )
}
