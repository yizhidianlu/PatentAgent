import { forwardRef, type ButtonHTMLAttributes } from 'react'
import { cn } from '../../lib/cn'

export type ButtonVariant = 'primary' | 'gradient' | 'secondary' | 'ghost' | 'danger'
export type ButtonSize = 'sm' | 'md'

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
  size?: ButtonSize
}

const base =
  'inline-flex items-center justify-center gap-2 select-none whitespace-nowrap ' +
  'transition-colors duration-150 focus-visible:outline-none focus-visible:ring-4 ' +
  'focus-visible:ring-indigo-500/20 disabled:opacity-50 disabled:pointer-events-none'

const variants: Record<ButtonVariant, string> = {
  primary:
    'bg-[#6366f1] hover:bg-[#5558e6] text-white rounded-xl px-4 py-2 text-sm font-medium shadow-sm active:scale-[0.99]',
  gradient:
    'bg-gradient-to-r from-[#61d0e2] via-[#492497] to-[#d13870] text-white rounded-full px-5 py-2 ' +
    'font-semibold shadow-lg shadow-indigo-500/30 hover:scale-105 hover:shadow-xl ' +
    'transition-all duration-300 active:scale-[0.99]',
  secondary:
    'bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-200 rounded-xl px-4 py-2 text-sm font-medium ' +
    'border border-gray-300 dark:border-gray-600 shadow-sm hover:bg-gray-50 dark:hover:bg-gray-700 active:scale-[0.99]',
  ghost:
    'bg-transparent text-gray-600 dark:text-gray-300 rounded-xl px-4 py-2 text-sm font-medium ' +
    'hover:bg-gray-100 dark:hover:bg-gray-800 active:scale-[0.99]',
  danger:
    'bg-red-600 hover:bg-red-700 text-white rounded-xl px-4 py-2 text-sm font-medium shadow-sm active:scale-[0.99]',
}

const sizes: Record<ButtonSize, string> = {
  sm: 'px-3 py-1.5 text-xs',
  md: '',
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = 'primary', size = 'md', className, type = 'button', ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      className={cn(base, variants[variant], sizes[size], className)}
      {...rest}
    />
  )
})
