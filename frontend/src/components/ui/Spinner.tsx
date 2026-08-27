import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'

export interface SpinnerProps {
  size?: 'sm' | 'md' | 'lg'
  className?: string
}

const sizes = {
  sm: 'w-4 h-4 border-2',
  md: 'w-5 h-5 border-2',
  lg: 'w-8 h-8 border-[3px]',
} as const

export function Spinner({ size = 'md', className }: SpinnerProps) {
  return (
    <span
      role="status"
      aria-label={zh.common.loading}
      className={cn(
        'inline-block rounded-full border-gray-300 dark:border-gray-600 border-t-[#6366f1] animate-spin',
        sizes[size],
        className,
      )}
    />
  )
}
