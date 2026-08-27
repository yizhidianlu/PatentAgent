import type { HTMLAttributes } from 'react'
import { cn } from '../../lib/cn'

export interface SkeletonProps extends HTMLAttributes<HTMLDivElement> {
  /** shimmer 变体：after 渐变扫过（--animate-shimmer）。 */
  shimmer?: boolean
}

export function Skeleton({ shimmer = false, className, ...rest }: SkeletonProps) {
  return (
    <div
      aria-hidden="true"
      className={cn(
        'rounded-lg bg-gray-200/80 dark:bg-gray-700/60 animate-pulse',
        shimmer &&
          'relative overflow-hidden animate-none ' +
            'after:absolute after:inset-0 after:animate-shimmer ' +
            'after:bg-gradient-to-r after:from-transparent after:via-white/60 after:to-transparent ' +
            'dark:after:via-white/10',
        className,
      )}
      {...rest}
    />
  )
}
