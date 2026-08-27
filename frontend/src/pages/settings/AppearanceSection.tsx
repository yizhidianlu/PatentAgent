import { useState, type ComponentType, type SVGProps } from 'react'
import {
  CheckIcon,
  ChevronDownIcon,
  ClockIcon,
  ComputerDesktopIcon,
  MoonIcon,
  SunIcon,
} from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { Select } from '../../components/ui/Select'
import { Spinner } from '../../components/ui/Spinner'
import { useUiStore } from '../../stores/uiStore'
import type { ThemeMode } from '../../lib/theme'
import { useSystemEnv, type SystemEnv } from '../../api/settings'
import { Field, SettingsCard, StatusBadge } from './SettingsPrimitives'

type IconType = ComponentType<SVGProps<SVGSVGElement>>

const THEME_TILES: { mode: ThemeMode; Icon: IconType }[] = [
  { mode: 'light', Icon: SunIcon },
  { mode: 'dark', Icon: MoonIcon },
  { mode: 'system', Icon: ComputerDesktopIcon },
  { mode: 'auto', Icon: ClockIcon },
]

/** 磁盘余量低于此值（GB）标红。 */
const LOW_DISK_GB = 5

interface EnvRow {
  key: string
  label: string
  ok: boolean
  detail?: string | null
  badgeText?: string
}

function buildEnvRows(env: SystemEnv): EnvRow[] {
  const items = zh.settings.appearance.envItems
  const hints = zh.settings.appearance.envHints
  const disk = env.data_dir.disk
  const freeGb = typeof disk.free_gb === 'number' ? disk.free_gb : null
  return [
    { key: 'word', label: items.word, ok: env.word.available, detail: env.word.path ?? hints.word },
    {
      key: 'soffice',
      label: items.soffice,
      ok: env.soffice.available,
      detail: env.soffice.path ?? hints.soffice,
    },
    {
      key: 'chrome',
      label: items.chrome,
      ok: env.chrome.available,
      detail: env.chrome.path ?? hints.chrome,
    },
    { key: 'edge', label: items.edge, ok: env.edge.available, detail: env.edge.path },
    {
      key: 'sqlite_vec',
      label: items.sqliteVec,
      ok: env.sqlite_vec.loaded,
      detail: env.sqlite_vec.loaded ? env.sqlite_vec.version : (env.sqlite_vec.error ?? hints.sqliteVec),
    },
    {
      key: 'playwright',
      label: items.playwright,
      ok: env.playwright.importable,
      detail: hints.playwright,
    },
    {
      key: 'disk',
      label: items.disk,
      ok: freeGb === null ? false : freeGb >= LOW_DISK_GB,
      detail:
        freeGb !== null && typeof disk.total_gb === 'number'
          ? zh.settings.appearance.diskFree(freeGb, disk.total_gb)
          : (disk.error ?? null),
      badgeText: freeGb !== null ? `${freeGb} GB` : undefined,
    },
    {
      key: 'python',
      label: items.python,
      ok: true,
      detail: env.python.platform,
      badgeText: env.python.version,
    },
  ]
}

/** §3.7 AppearanceSection：主题四 radio tiles + 语言 + 数据目录 + 系统环境自检。 */
export function AppearanceSection() {
  const themeMode = useUiStore((s) => s.themeMode)
  const setThemeMode = useUiStore((s) => s.setThemeMode)
  const [envOpen, setEnvOpen] = useState(false)
  const envQuery = useSystemEnv()

  const env = envQuery.data
  const rows = env ? buildEnvRows(env) : []

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

      <Field label={zh.settings.appearance.dataDir} hint={zh.settings.appearance.dataDirHint}>
        {(id) => (
          <div
            id={id}
            className="rounded-xl border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900/40 px-3.5 py-2.5 text-xs font-mono text-gray-600 dark:text-gray-300 break-all"
          >
            {envQuery.isLoading
              ? zh.settings.appearance.envLoading
              : (env?.data_dir.path ?? zh.settings.appearance.envFailed)}
          </div>
        )}
      </Field>

      <div className="rounded-xl border border-gray-200 dark:border-gray-700 overflow-hidden">
        <button
          type="button"
          onClick={() => setEnvOpen((v) => !v)}
          aria-expanded={envOpen}
          className="w-full flex items-center justify-between gap-3 px-3.5 py-2.5 text-sm font-medium text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-700/50 transition-colors"
        >
          {envOpen ? zh.settings.appearance.envTitleOpen : zh.settings.appearance.envTitle}
          <ChevronDownIcon
            className={cn('w-4 h-4 text-gray-400 transition-transform duration-200', envOpen && 'rotate-180')}
            strokeWidth={1.5}
          />
        </button>
        {envOpen && (
          <div className="border-t border-gray-200 dark:border-gray-700 px-3.5 py-1">
            {envQuery.isLoading && (
              <p className="flex items-center gap-2 py-3 text-xs text-gray-500 dark:text-gray-400">
                <Spinner size="sm" />
                {zh.settings.appearance.envLoading}
              </p>
            )}
            {envQuery.isError && (
              <p className="py-3 text-xs text-red-500">{zh.settings.appearance.envFailed}</p>
            )}
            {rows.map((row) => (
              <div
                key={row.key}
                className="flex items-center justify-between gap-3 py-2.5 border-b border-gray-100 dark:border-gray-700/60 last:border-0"
              >
                <div className="min-w-0">
                  <p className="text-sm text-gray-700 dark:text-gray-200">{row.label}</p>
                  {row.detail && (
                    <p className="mt-0.5 text-xs text-gray-400 dark:text-gray-500 truncate">
                      {row.detail}
                    </p>
                  )}
                </div>
                <StatusBadge ok={row.ok} okText={row.badgeText} failText={row.badgeText} />
              </div>
            ))}
          </div>
        )}
      </div>
    </SettingsCard>
  )
}
