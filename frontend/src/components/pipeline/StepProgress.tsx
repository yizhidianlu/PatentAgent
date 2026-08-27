import { CheckIcon, ExclamationTriangleIcon } from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import type { PipelineStepState } from '../../stores/sessionStore'

/** 展示用步骤定义：一个展示步骤可聚合多个后端 step key。 */
export interface DisplayStepPreset {
  key: string
  label: string
  /** 聚合的后端 step keys（缺省 = [key]）。 */
  matchKeys?: string[]
}

export type DisplayStepStatus = 'done' | 'current' | 'pending' | 'failed'

export interface DisplayStep {
  key: string
  label: string
  status: DisplayStepStatus
}

/**
 * pipeline.steps → 展示步骤映射：
 * - 任一聚合 key running/waiting_user → current；
 * - 聚合 keys 全部 done/skipped → done；
 * - 后端缺失的展示步骤，凡后续步骤已有进展即视为 done（隐含完成）。
 */
export function computeDisplaySteps(
  presets: DisplayStepPreset[],
  pipelineSteps: PipelineStepState[],
): DisplayStep[] {
  const statusMap = new Map(pipelineSteps.map((s) => [s.key, s.status]))

  const raw = presets.map((preset) => {
    const keys = preset.matchKeys ?? [preset.key]
    const statuses = keys
      .map((k) => statusMap.get(k))
      .filter((s): s is NonNullable<typeof s> => s !== undefined)
    const current = statuses.some((s) => s === 'running' || s === 'waiting_user')
    const failed = !current && statuses.some((s) => s === 'failed')
    const done = statuses.length > 0 && statuses.every((s) => s === 'done' || s === 'skipped')
    const hasProgress = current || statuses.length > 0
    return { preset, current, failed, done, hasProgress }
  })

  // 隐含完成：后面的展示步骤有任何进展 → 前面未匹配的步骤视为 done
  let laterProgress = false
  for (let i = raw.length - 1; i >= 0; i -= 1) {
    const r = raw[i]
    if (!r.current && !r.failed && !r.done && laterProgress) r.done = true
    if (r.hasProgress || r.done) laterProgress = true
  }

  return raw.map((r) => ({
    key: r.preset.key,
    label: r.preset.label,
    status: r.current ? 'current' : r.failed ? 'failed' : r.done ? 'done' : 'pending',
  }))
}

/**
 * 动态步骤条（交底书等「步骤集随分支增减」的模块）：
 * 以后端 pipeline.steps 为准渲染，presets 仅提供顺序骨架与名称美化；
 * 后端出现 presets 未覆盖的 step_key（如实用/外观分支的 schema_fill）时，
 * 按其出现位置插入，标签取 labels[key] → 后端 name_zh → key。
 */
export function computeDynamicSteps(
  presets: DisplayStepPreset[],
  pipelineSteps: PipelineStepState[],
  labels: Record<string, string> = {},
): DisplayStep[] {
  const keys = presets.map((p) => p.key)

  // 未覆盖的后端 step_key：插到上一个已匹配步骤之后（保序）
  let anchor = -1
  for (const step of pipelineSteps) {
    const covered = presets.findIndex((p) => (p.matchKeys ?? [p.key]).includes(step.key))
    if (covered >= 0) {
      anchor = keys.indexOf(presets[covered].key)
      continue
    }
    const existing = keys.indexOf(step.key)
    if (existing >= 0) {
      anchor = existing
      continue
    }
    anchor += 1
    keys.splice(anchor, 0, step.key)
  }

  const nameByKey = new Map(pipelineSteps.map((s) => [s.key, s.nameZh]))
  const merged: DisplayStepPreset[] = keys.map((key) => {
    const preset = presets.find((p) => p.key === key)
    if (preset) return preset
    return { key, label: labels[key] ?? nameByKey.get(key) ?? key }
  })

  return computeDisplaySteps(merged, pipelineSteps)
}

export interface StepProgressProps {
  steps: DisplayStep[]
  className?: string
}

/**
 * §2.8 StepProgress：sticky 渐变横幅 + 步骤圆（done 靛蓝✓ / current 白底描边
 * pulse-glow / pending 灰）+ 连接线；移动端紧凑文本「当前步骤 3/8 · xxx」。
 */
export function StepProgress({ steps, className }: StepProgressProps) {
  if (steps.length === 0) return null
  const failedIndex = steps.findIndex((s) => s.status === 'failed')
  const currentIndex = steps.findIndex((s) => s.status === 'current')
  const activeIndex =
    failedIndex >= 0
      ? failedIndex
      : currentIndex >= 0
        ? currentIndex
        : steps.findIndex((s) => s.status === 'pending')
  const compact =
    failedIndex >= 0
      ? zh.pipeline.stepFailed(steps[failedIndex].label)
      : activeIndex >= 0
        ? zh.pipeline.mobileStep(activeIndex + 1, steps.length, steps[activeIndex].label)
        : zh.pipeline.mobileStep(steps.length, steps.length, steps[steps.length - 1].label)

  return (
    <div
      aria-label={zh.pipeline.progressLabel}
      className={cn(
        'sticky top-0 z-30 border-b border-blue-100 dark:border-gray-700',
        'bg-gradient-to-r from-blue-50 to-indigo-50 dark:from-gray-800/80 dark:to-gray-800/80',
        'px-4 py-1',
        className,
      )}
    >
      {/* 桌面：横向步骤条 */}
      <div className="hidden sm:flex max-w-[800px] mx-auto items-center h-9 overflow-x-auto scrollbar-thin">
        {steps.map((step, i) => {
          const done = step.status === 'done'
          const current = step.status === 'current'
          const failed = step.status === 'failed'
          return (
            <div key={step.key} className="flex items-center shrink-0">
              {i > 0 && (
                <span
                  className={cn(
                    'h-0.5 w-6 rounded mr-3',
                    failed
                      ? 'bg-red-400'
                      : done || current
                        ? 'bg-[#6366f1]'
                        : 'bg-gray-300 dark:bg-gray-600',
                  )}
                  aria-hidden="true"
                />
              )}
              <span
                title={failed ? zh.pipeline.stepFailed(step.label) : step.label}
                className={cn(
                  'w-6 h-6 rounded-full text-[10px] font-semibold flex items-center justify-center shrink-0',
                  done && 'bg-[#6366f1] text-white',
                  failed && 'bg-red-500 text-white',
                  current &&
                    'bg-white dark:bg-gray-900 border-2 border-[#6366f1] text-[#6366f1] animate-pulse-glow',
                  !done &&
                    !current &&
                    !failed &&
                    'bg-gray-200 dark:bg-gray-700 text-gray-500 dark:text-gray-400',
                )}
              >
                {done ? (
                  <CheckIcon className="w-3.5 h-3.5" strokeWidth={2.5} />
                ) : failed ? (
                  <ExclamationTriangleIcon className="w-3.5 h-3.5" strokeWidth={2.5} />
                ) : (
                  i + 1
                )}
              </span>
              <span
                className={cn(
                  'text-xs ml-1.5 mr-3 whitespace-nowrap',
                  failed
                    ? 'font-medium text-red-600 dark:text-red-400'
                    : current
                      ? 'font-medium text-gray-900 dark:text-gray-100'
                      : done
                        ? 'text-gray-600 dark:text-gray-300'
                        : 'text-gray-400 dark:text-gray-500',
                )}
              >
                {step.label}
              </span>
            </div>
          )
        })}
      </div>

      {/* 移动端：紧凑文本 */}
      <div
        className={cn(
          'sm:hidden h-9 flex items-center justify-center text-xs',
          failedIndex >= 0
            ? 'font-medium text-red-600 dark:text-red-400'
            : 'text-gray-600 dark:text-gray-300',
        )}
      >
        {compact}
      </div>
    </div>
  )
}
