export interface ChipPreviewCardProps {
  title: string
  description: string
}

/**
 * §2.5 悬停预览卡（300ms 延迟后出现）：
 * `absolute top-full mt-2 max-w-[300px] sm:max-w-[340px] rounded-xl border shadow-sm`
 * animate-in fade-in zoom-in-95；体 px-3.5 py-3。
 */
export function ChipPreviewCard({ title, description }: ChipPreviewCardProps) {
  return (
    <div
      className={
        'absolute top-full left-1/2 -translate-x-1/2 mt-2 w-max max-w-[300px] sm:max-w-[340px] ' +
        'rounded-xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 shadow-sm z-20 ' +
        'animate-in fade-in zoom-in-95 duration-200'
      }
    >
      <div className="px-3.5 py-3 text-left">
        <p className="text-sm font-semibold text-gray-900 dark:text-white">{title}</p>
        <p className="text-xs text-gray-500 dark:text-gray-400 mt-1 leading-relaxed">
          {description}
        </p>
      </div>
    </div>
  )
}
