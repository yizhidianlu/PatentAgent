import { type ComponentType, type SVGProps } from 'react'
import {
  CheckIcon,
  ClockIcon,
  ComputerDesktopIcon,
  MoonIcon,
  SunIcon,
} from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { Select } from '../../components/ui/Select'
import { useUiStore } from '../../stores/uiStore'
import type { ThemeMode } from '../../lib/theme'
import { Field, SettingsCard } from './SettingsPrimitives'

type IconType = ComponentType<SVGProps<SVGSVGElement>>

const THEME_TILES: { mode: ThemeMode; Icon: IconType }[] = [
  { mode: 'light', Icon: SunIcon },
  { mode: 'dark', Icon: MoonIcon },
  { mode: 'system', Icon: ComputerDesktopIcon },
  { mode: 'auto', Icon: ClockIcon },
]

/** §3.7 AppearanceSection：主题四 radio tiles + 语言。
 *
 * 数据目录与系统环境自检**不在这里**——它们是服务器内部事实，已迁到仅管理员
 * 可见的 SystemSection。本节人人可见，别再往里塞服务器信息。 */
export function AppearanceSection() {
  const themeMode = useUiStore((s) => s.themeMode)
  const setThemeMode = useUiStore((s) => s.setThemeMode)
  return (
    <SettingsCard title={zh.settings.appearance.title} description={zh.settings.appearance.desc}>
      <Field label={zh.settings.appearance.theme}>
        {() => (
          <div role="radiogroup" aria-label={zh.settings.appearance.theme} className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            {THEME_TILES.map(({ mode, Icon }) => {
              const active = themeMode === mode
              return (
                <button
                  key={mode}
                  type="button"
                  role="radio"
                  aria-checked={active}
                  onClick={() => setThemeMode(mode)}
                  className={cn(
                    'rounded-xl border-2 p-3.5 text-left transition-colors',
                    active
                      ? 'border-[#6366f1] bg-indigo-50/50 dark:bg-indigo-500/10'
                      : 'border-gray-200 dark:border-gray-700 hover:border-[#6366f1]/40',
                  )}
                >
                  <span className="flex items-center gap-2 text-sm font-medium text-gray-800 dark:text-gray-100">
                    <Icon className="w-4 h-4" strokeWidth={1.5} />
                    {zh.settings.appearance.themeModes[mode]}
                    {active && <CheckIcon className="ml-auto w-4 h-4 text-[#6366f1]" strokeWidth={2} />}
                  </span>
                  <span className="mt-1 block text-xs text-gray-500 dark:text-gray-400">
                    {zh.settings.appearance.themeModeDescs[mode]}
                  </span>
                </button>
              )
            })}
          </div>
        )}
      </Field>

      <Field label={zh.settings.appearance.language} hint={zh.settings.appearance.languageHint}>
        {(id) => (
          <Select id={id} value="zh" disabled onChange={() => undefined}>
            <option value="zh">{zh.settings.appearance.languageZh}</option>
          </Select>
        )}
      </Field>

    </SettingsCard>
  )
}
