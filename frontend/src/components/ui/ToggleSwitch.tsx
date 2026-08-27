import { cn } from '../../lib/cn'

export interface ToggleSwitchProps {
  checked: boolean
  onChange: (checked: boolean) => void
  disabled?: boolean
  /** 无障碍标签。 */
  label?: string
  className?: string
}

/** §2.6：`h-5 w-9 rounded-full`，on `bg-[#6366f1]`，knob `h-4 w-4 bg-white shadow translate-x-0.5→[18px]`。 */
export function ToggleSwitch({ checked, onChange, disabled = false, label, className }: ToggleSwitchProps) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn(
        'relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors duration-200',
        'focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-indigo-500/20',
        checked ? 'bg-[#6366f1]' : 'bg-gray-300 dark:bg-gray-600',
        disabled && 'opacity-50 cursor-not-allowed',
        className,
      )}
    >
      <span
        className={cn(
          'inline-block h-4 w-4 rounded-full bg-white shadow transition-transform duration-200',
          checked ? 'translate-x-[18px]' : 'translate-x-0.5',
        )}
      />
    </button>
  )
}
