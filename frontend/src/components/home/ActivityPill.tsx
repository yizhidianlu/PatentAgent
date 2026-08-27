import { SparklesIcon } from '@heroicons/react/24/outline'
import { zh } from '../../i18n/zh'
import { cn } from '../../lib/cn'

export interface ActivityPillProps {
  /** 点击（默认用于切到「论文转专利」模块）。 */
  onClick?: () => void
  className?: string
}

/**
 * §3.1 ActivityPill：品牌渐变胶囊 + SparklesIcon animate-pulse，
 * 文案「全新上线 · 论文一键转专利」；hover scale 1.05 + shadow-xl（§7 渐变胶囊行）。
 * 高度 36px（参考站实测胶囊行 h=36：text-sm + py-2）。
 */
export function ActivityPill({ onClick, className }: ActivityPillProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        'inline-flex items-center gap-1.5 rounded-full px-4 py-2 text-sm font-semibold text-white',
        'bg-gradient-to-r from-[#61d0e2] via-[#492497] to-[#d13870]',
        'shadow-lg shadow-indigo-500/30 hover:scale-105 hover:shadow-xl',
        'transition-all duration-300 active:scale-[0.99]',
        className,
      )}
    >
      <SparklesIcon className="w-4 h-4 shrink-0 animate-pulse" strokeWidth={1.5} />
      {zh.home.activityPill}
    </button>
  )
}
