import { useId, type ComponentType, type SVGProps } from 'react'
import { motion } from 'framer-motion'
import { cn } from '../../lib/cn'

export interface SegmentedOption<T extends string> {
  value: T
  label: string
  /** 可选前置图标（Composer 工具栏内的分段器用简称 + 图标，gap-1.5）。 */
  icon?: ComponentType<SVGProps<SVGSVGElement>>
}

export interface SegmentedToggleProps<T extends string> {
  options: readonly SegmentedOption<T>[]
  value: T
  onChange: (value: T) => void
  /** 无障碍：tablist 名称。 */
  ariaLabel?: string
  className?: string
}

/**
 * §2.4 SegmentedToggle（参考站 Playwright 实测）：
 * 轨道 `flex items-center gap-1 bg-gray-100 dark:bg-gray-800 rounded-full p-0.5 h-9 relative`；
 * 段钮 `relative flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-medium transition-colors duration-200`；
 * thumb = motion layoutId `absolute top-0.5 bottom-0.5 rounded-full bg-white dark:bg-gray-600 shadow-sm` .3s ease-smooth。
 */
export function SegmentedToggle<T extends string>({
  options,
  value,
  onChange,
  ariaLabel,
  className,
}: SegmentedToggleProps<T>) {
  const layoutId = useId()

  return (
    <div
      role="tablist"
      aria-label={ariaLabel}
      className={cn(
        'relative flex items-center gap-1 bg-gray-100 dark:bg-gray-800 rounded-full p-0.5 h-9',
        className,
      )}
    >
      {options.map((opt) => {
        const active = opt.value === value
        const Icon = opt.icon
        return (
          <button
            key={opt.value}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onChange(opt.value)}
            className={cn(
              'relative flex items-center gap-1.5 px-3 py-1.5 rounded-full',
              'text-xs font-medium transition-colors duration-200 whitespace-nowrap',
              active
                ? 'text-gray-900 dark:text-white'
                : 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300',
            )}
          >
            {active && (
              <motion.span
                layoutId={layoutId}
                className="absolute inset-x-0 top-0.5 bottom-0.5 -z-0 rounded-full bg-white dark:bg-gray-600 shadow-sm"
                transition={{ duration: 0.3, ease: [0.25, 0.1, 0.25, 1] }}
              />
            )}
            {Icon && <Icon className="relative z-10 w-3.5 h-3.5 shrink-0" strokeWidth={1.5} />}
            <span className="relative z-10">{opt.label}</span>
          </button>
        )
      })}
    </div>
  )
}
