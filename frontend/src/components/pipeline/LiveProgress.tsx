import { useEffect, useState } from 'react'
import { ClockIcon, ExclamationTriangleIcon, PauseCircleIcon, SignalSlashIcon } from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import type { SseConnectionState } from '../../lib/sse'
import type { StepProgressEvent } from '../../types/stream'

/** 心跳间隔（后端 services/progress.BEAT_SEC）。 */
const BEAT_MS = 5000
/** 连续丢多少拍就认为事件流本身断了，而不是后端卡住。 */
const MISSED_BEATS_OFFLINE = 3

function formatDuration(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  if (h > 0) return `${h} 小时 ${m} 分`
  if (m > 0) return `${m} 分 ${s} 秒`
  return `${s} 秒`
}

export interface LiveProgressProps {
  progress: StepProgressEvent | null
  /** 最近一次收到心跳的本地时刻；用来区分「后端卡住」与「事件流断了」。 */
  progressAt: number | null
  connection: SseConnectionState
  className?: string
}

/**
 * 步骤内实时进度条。
 *
 * 要回答的是三个问题，缺一个这个组件就白做：
 *
 * 1. **它在跑吗** —— 耗时在走、心跳在来，就是在跑；
 * 2. **跑到哪了** —— 阶段名 + `index/total`（分子来自后端真实的循环变量，
 *    前端绝不自行估算百分比：假进度条停在 90% 不动时，用户失去的是对整个系统的信任）；
 * 3. **卡住了是卡在哪** —— 三种「不动」长得很像，但处置完全不同，必须分开说：
 *    - 等用户确认（suspended）：不是异常，别报警；
 *    - 等外部响应超时（stalled）：告诉他在等谁、等了多久、可以怎么办；
 *    - 事件流断了（心跳不来了）：任务其实还在服务器上跑，必须说清楚，
 *      否则用户会去重启或重跑，那才真的会丢掉几十分钟的产出。
 */
export function LiveProgress({ progress, progressAt, connection, className }: LiveProgressProps) {
  // 心跳 5 秒一拍，中间要靠本地时钟把耗时走起来，否则界面看着像卡住了
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!progress) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [progress])

  if (!progress) return null

  const sinceBeat = progressAt === null ? 0 : now - progressAt
  const offline =
    connection === 'reconnecting' ||
    connection === 'failed' ||
    connection === 'closed' ||
    sinceBeat > BEAT_MS * MISSED_BEATS_OFFLINE

  // 本地推进：耗时/静默时长都从最后一拍往前推，界面才不会一顿一顿的。
  // 掉线时停止外推 —— 那时这两个数字已经不代表服务器上的真实情况了。
  const drift = offline ? 0 : sinceBeat
  const elapsed = progress.elapsed_ms + drift
  const idle = progress.idle_ms + drift

  const hasCount =
    typeof progress.index === 'number' &&
    typeof progress.total === 'number' &&
    progress.total > 0
  const percent = hasCount
    ? Math.min(100, Math.round(((progress.index as number) / (progress.total as number)) * 100))
    : 0

  const headline = progress.phase || progress.name_zh || zh.pipeline.live.running

  return (
    <div
      aria-label={zh.pipeline.live.label}
      aria-live="polite"
      className={cn(
        'shrink-0 px-4 py-2 border-b text-xs',
        'border-gray-100 dark:border-gray-800',
        className,
      )}
    >
      <div className="flex items-center gap-2 min-w-0">
        <ClockIcon className="w-4 h-4 shrink-0 text-gray-400" strokeWidth={1.5} />
        <span className="truncate min-w-0 flex-1 text-gray-700 dark:text-gray-200">
          {headline}
        </span>
        {hasCount && (
          <span className="shrink-0 tabular-nums font-medium text-primary-500">
            {zh.pipeline.live.ofTotal(progress.index as number, progress.total as number)}
          </span>
        )}
        <span className="shrink-0 tabular-nums text-gray-400">
          {zh.pipeline.live.elapsed(formatDuration(elapsed))}
        </span>
      </div>

      {/* 进度条：给得出真实分子才画确定进度；给不出就画一条流动条，
          它只表示「在跑」，不表示「跑了多少」——后者我们不知道，就不假装知道。 */}
      <div className="mt-1.5 h-1 rounded-full bg-gray-100 dark:bg-gray-800 overflow-hidden">
        {hasCount ? (
          <div
            className={cn(
              'h-full rounded-full transition-[width] duration-500 ease-smooth',
              progress.suspended || offline ? 'bg-gray-300 dark:bg-gray-600' : 'bg-primary-500',
            )}
            style={{ width: `${percent}%` }}
          />
        ) : (
          <div
            className={cn(
              'h-full w-1/3 rounded-full',
              progress.suspended || offline
                ? 'bg-gray-300 dark:bg-gray-600'
                : 'bg-primary-500/60',
              // 只有「确实在动」时才让它动。掉线时数字已经不可信，
              // 挂起时本来就什么都没在跑 —— 这两种情况下继续animate 是在骗人
              !offline && !progress.suspended && 'animate-shimmer',
            )}
          />
        )}
      </div>

      {progress.detail && (
        <p className="mt-1 truncate text-[11px] text-gray-400">{progress.detail}</p>
      )}

      {offline ? (
        <Notice tone="amber" icon={SignalSlashIcon} title={zh.pipeline.live.offlineTitle}>
          {zh.pipeline.live.offlineHint}
        </Notice>
      ) : progress.suspended ? (
        <Notice tone="neutral" icon={PauseCircleIcon} title={zh.pipeline.live.suspended} />
      ) : progress.stalled ? (
        <Notice tone="amber" icon={ExclamationTriangleIcon} title={zh.pipeline.live.stalledTitle}>
          {progress.stall_hint}
        </Notice>
      ) : progress.waiting_for && idle > BEAT_MS * 2 ? (
        // 还没到「卡住」阈值，但也确实静了一会儿：如实说在等谁、静了多久，
        // 不报警也不装作一切正常
        <p className="mt-1 text-[11px] text-gray-400">
          {zh.pipeline.live.waitingFor(progress.waiting_for)} ·{' '}
          {zh.pipeline.live.idleFor(formatDuration(idle))}
        </p>
      ) : null}
    </div>
  )
}

function Notice({
  tone,
  icon: Icon,
  title,
  children,
}: {
  tone: 'amber' | 'neutral'
  icon: typeof ClockIcon
  title: string
  children?: React.ReactNode
}) {
  return (
    <div
      className={cn(
        'mt-1.5 flex items-start gap-1.5 rounded-lg px-2 py-1.5 text-[11px]',
        tone === 'amber'
          ? 'bg-amber-50 dark:bg-amber-500/10 text-amber-700 dark:text-amber-400'
          : 'bg-gray-50 dark:bg-gray-800/60 text-gray-500 dark:text-gray-400',
      )}
      role="status"
    >
      <Icon className="w-3.5 h-3.5 shrink-0 mt-px" strokeWidth={1.5} />
      <span className="min-w-0">
        <span className="font-medium">{title}</span>
        {children ? <span className="block mt-0.5 opacity-90">{children}</span> : null}
      </span>
    </div>
  )
}
