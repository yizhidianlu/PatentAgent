import { ArrowUpIcon, StopIcon } from '@heroicons/react/24/outline'
import { zh } from '../../i18n/zh'
import { cn } from '../../lib/cn'

export type SendButtonState = 'disabled' | 'ready' | 'busy'

export interface SendButtonProps {
  state: SendButtonState
  accent?: 'indigo' | 'orange'
  onClick: () => void
}

/**
 * §2.3 SendButton：`w-9 h-9 rounded-full` 三态 ——
 * disabled 灰 / ready `bg-[#6366f1] shadow-lg shadow-indigo-500/30`（橙变体）/ busy 显 StopIcon。
 */
export function SendButton({ state, accent = 'indigo', onClick }: SendButtonProps) {
  const readyCls =
    accent === 'orange'
      ? 'bg-orange-500 hover:bg-orange-600 text-white shadow-lg shadow-orange-500/30'
      : 'bg-[#6366f1] hover:bg-[#5558e6] text-white shadow-lg shadow-indigo-500/30'

  return (
    <button
      type="button"
      aria-label={state === 'busy' ? zh.composer.stop : zh.composer.send}
      disabled={state === 'disabled'}
      onClick={onClick}
      className={cn(
        'w-9 h-9 rounded-full flex items-center justify-center transition-all duration-200 active:scale-95',
        state === 'disabled' &&
          'bg-gray-200 dark:bg-gray-700 text-gray-400 dark:text-gray-500 cursor-not-allowed',
        state !== 'disabled' && readyCls,
      )}
    >
      {state === 'busy' ? (
        <StopIcon className="w-4 h-4" strokeWidth={2} />
      ) : (
        <ArrowUpIcon className="w-4 h-4" strokeWidth={2} />
      )}
    </button>
  )
}
