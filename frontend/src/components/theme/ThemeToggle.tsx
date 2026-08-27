import {
  CheckIcon,
  ClockIcon,
  ComputerDesktopIcon,
  MoonIcon,
  SunIcon,
} from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { useUiStore } from '../../stores/uiStore'
import type { ThemeMode } from '../../lib/theme'
import { Dropdown, DropdownItem } from '../ui/Dropdown'

const modes: { value: ThemeMode; label: string; Icon: typeof SunIcon }[] = [
  { value: 'light', label: zh.theme.light, Icon: SunIcon },
  { value: 'dark', label: zh.theme.dark, Icon: MoonIcon },
  { value: 'system', label: zh.theme.system, Icon: ComputerDesktopIcon },
  { value: 'auto', label: zh.theme.auto, Icon: ClockIcon },
]

/** §2.6 ThemeToggle：下拉 w-64 四行，active 行 CheckIcon text-[#6366f1]。 */
export function ThemeToggle() {
  const themeMode = useUiStore((s) => s.themeMode)
  const setThemeMode = useUiStore((s) => s.setThemeMode)
  const CurrentIcon = modes.find((m) => m.value === themeMode)?.Icon ?? SunIcon

  return (
    <Dropdown
      align="right"
      panelClassName="w-64"
      trigger={({ open, toggle }) => (
        <button
          type="button"
          onClick={toggle}
          aria-label={zh.theme.toggleLabel}
          aria-expanded={open}
          className={cn(
            'w-8 h-8 rounded-full flex items-center justify-center text-gray-600 dark:text-gray-300 ' +
              'hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors',
            open && 'bg-gray-100 dark:bg-gray-800',
          )}
        >
          <CurrentIcon className="w-5 h-5" strokeWidth={1.5} />
        </button>
      )}
    >
      {({ close }) => (
        <>
          {modes.map(({ value, label, Icon }) => (
            <DropdownItem
              key={value}
              onClick={() => {
                setThemeMode(value)
                close()
              }}
            >
              <Icon className="w-4 h-4 text-gray-400 dark:text-gray-500" strokeWidth={1.5} />
              <span className="flex-1">{label}</span>
              {themeMode === value && <CheckIcon className="w-4 h-4 text-[#6366f1]" strokeWidth={2} />}
            </DropdownItem>
          ))}
        </>
      )}
    </Dropdown>
  )
}
