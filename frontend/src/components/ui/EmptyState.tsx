import type { ComponentType, ReactNode, SVGProps } from 'react'
import { InboxIcon } from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'

export interface EmptyStateProps {
  icon?: ComponentType<SVGProps<SVGSVGElement>>
  title: string
  description?: string
  /** 可选 CTA（一般放 Button）。 */
  action?: ReactNode
  className?: string
}

export function EmptyState({ icon: Icon = InboxIcon, title, description, action, className }: EmptyStateProps) {
  return (
    <div className={cn('flex flex-col items-center justify-center text-center gap-3 py-10 px-4', className)}>
      <div className="w-14 h-14 rounded-2xl bg-gray-100 dark:bg-gray-800 flex items-center justify-center">
        <Icon className="w-7 h-7 text-gray-400 dark:text-gray-500" strokeWidth={1.5} />
      </div>
      <div className="space-y-1">
        <p className="text-sm font-medium text-gray-900 dark:text-gray-100">{title}</p>
        {description && <p className="text-xs text-gray-500 dark:text-gray-400 max-w-xs">{description}</p>}
      </div>
      {action}
    </div>
  )
}
