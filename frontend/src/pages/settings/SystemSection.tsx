import { useState, type ComponentType, type SVGProps } from 'react'
import { ChevronDownIcon } from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { Spinner } from '../../components/ui/Spinner'
import { useSystemEnv, type SystemEnv } from '../../api/settings'
import { Field, SettingsCard, StatusBadge } from './SettingsPrimitives'

export type IconType = ComponentType<SVGProps<SVGSVGElement>>

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
      key: 'sqliteVec',
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
      ok: typeof disk.free_gb === 'number' ? disk.free_gb >= LOW_DISK_GB : false,
      detail:
        typeof disk.free_gb === 'number' && typeof disk.total_gb === 'number'
          ? zh.settings.appearance.diskFree(disk.free_gb, disk.total_gb)
          : disk.error,
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

/**
 * §3.7 SystemSection：数据目录 + 系统环境自检。**平台级，仅管理员可见。**
 *
 * 这一块原本挂在人人可见的「外观」节里。它 report 的每一项都是服务器内部事实：
 * 数据目录绝对路径（连带泄露服务器账户名与目录结构）、Word/Chrome/Edge 的安装
 * 路径、操作系统精确版本、磁盘容量。对运维是诊断信息，对普通用户是侦察材料。
 * 后端 `/system/env` 已挂 require_admin 兜底，这里的隐藏只是别让它出现在导航里。
 */
export function SystemSection() {
  const [envOpen, setEnvOpen] = useState(true)
  const envQuery = useSystemEnv()

  const env = envQuery.data
  const rows = env ? buildEnvRows(env) : []

  return (
    <SettingsCard title={zh.settings.system.title} description={zh.settings.system.desc}>
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
