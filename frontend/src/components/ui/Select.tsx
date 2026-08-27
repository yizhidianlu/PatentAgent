import { forwardRef, type SelectHTMLAttributes } from 'react'
import { ChevronDownIcon } from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'

export type SelectProps = SelectHTMLAttributes<HTMLSelectElement>

export const Select = forwardRef<HTMLSelectElement, SelectProps>(function Select(
  { className, children, ...rest },
  ref,
) {
  return (
    <div className={cn('relative', className)}>
      <select
        ref={ref}
        className={cn(
          'h-10 w-full appearance-none rounded-xl border border-gray-300 dark:border-gray-600 ' +
            'bg-white dark:bg-gray-800 pl-3.5 pr-9 text-sm text-gray-900 dark:text-gray-100 ' +
            'outline-none transition-colors focus:border-[#6366f1] focus:ring-4 focus:ring-indigo-500/10 ' +
            'disabled:opacity-50 disabled:cursor-not-allowed',
        )}
        {...rest}
      >
        {children}
      </select>
      <ChevronDownIcon
        className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400"
        strokeWidth={1.5}
      />
    </div>
  )
})
