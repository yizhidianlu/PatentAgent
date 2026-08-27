import { forwardRef, type HTMLAttributes } from 'react'
import { cn } from '../../lib/cn'

export interface CardProps extends HTMLAttributes<HTMLDivElement> {
  /** accent 变体：`border-2 border-indigo-300 shadow-lg`。 */
  accent?: boolean
}

export const Card = forwardRef<HTMLDivElement, CardProps>(function Card(
  { accent = false, className, ...rest },
  ref,
) {
  return (
    <div
      ref={ref}
      className={cn(
        'rounded-2xl border border-gray-200/60 dark:border-gray-700/60 bg-white dark:bg-gray-800 shadow-sm',
        accent && 'border-2 border-indigo-300 dark:border-indigo-500/50 shadow-lg',
        className,
      )}
      {...rest}
    />
  )
})
