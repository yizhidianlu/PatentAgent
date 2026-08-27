import type { HTMLAttributes } from 'react'
import { cn } from '../../lib/cn'

export type BadgeVariant = 'neutral' | 'gradient' | 'emerald' | 'amber' | 'red' | 'indigo'

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  variant?: BadgeVariant
}

const variants: Record<BadgeVariant, string> = {
  neutral: 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300',
  gradient: 'bg-gradient-to-r from-blue-500 to-purple-500 text-white',
  emerald: 'bg-emerald-50 dark:bg-emerald-500/10 text-emerald-600 dark:text-emerald-400',
  amber: 'bg-amber-50 dark:bg-amber-500/10 text-amber-600 dark:text-amber-400',
  red: 'bg-red-50 dark:bg-red-500/10 text-red-600 dark:text-red-400',
  indigo: 'bg-indigo-50 dark:bg-indigo-500/10 text-[#6366f1] dark:text-indigo-400',
}

export function Badge({ variant = 'neutral', className, ...rest }: BadgeProps) {
  return (
    <span
      className={cn(
        'inline-flex items-center text-[10px] font-semibold px-1.5 py-0.5 rounded',
        variants[variant],
        className,
      )}
      {...rest}
    />
  )
}
