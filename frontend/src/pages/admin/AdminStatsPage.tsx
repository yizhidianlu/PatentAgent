import type { ComponentType, SVGProps } from 'react'
import {
  ArrowPathIcon,
  BoltIcon,
  CircleStackIcon,
  CpuChipIcon,
  DocumentDuplicateIcon,
  FolderIcon,
  UsersIcon,
} from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { useAdminStats, type AdminStats } from '../../api/admin'
import { Button } from '../../components/ui/Button'
import { Card } from '../../components/ui/Card'
import { Skeleton } from '../../components/ui/Skeleton'

const t = zh.admin.stats

type IconType = ComponentType<SVGProps<SVGSVGElement>>

interface StatTile {
  key: string
  label: string
  value: string
  /** 紧跟数字的单位（次 / MB）。 */
  unit?: string
  /** 数字下方的补充说明（整句，另起一行才读得顺）。 */
  note?: string
  Icon: IconType
  accent: string
}

function formatNumber(n: number): string {
  return n.toLocaleString('zh-CN')
}

/** 大数收敛为 12.3k / 4.5M，卡片里塞不下九位数。 */
function compact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 10_000) return `${(n / 1_000).toFixed(1)}k`
  return formatNumber(n)
}

function buildTiles(stats: AdminStats): StatTile[] {
  return [
    {
      key: 'users',
      label: t.cards.users,
      value: formatNumber(stats.user_count),
      note: t.activeUsers(stats.active_user_count),
      Icon: UsersIcon,
      accent: 'text-[#61d0e2] bg-[#61d0e2]/10',
    },
    {
      key: 'cases',
      label: t.cards.cases,
      value: formatNumber(stats.case_count),
      Icon: FolderIcon,
      accent: 'text-[#492497] dark:text-indigo-300 bg-[#492497]/10',
    },
    {
      key: 'calls',
      label: t.cards.llmCallsToday,
      value: formatNumber(stats.llm_calls_today),
      unit: t.unit.times,
      Icon: BoltIcon,
      accent: 'text-amber-500 bg-amber-500/10',
    },
    {
      key: 'tokens',
      label: t.cards.tokensThisMonth,
      value: compact(stats.tokens_this_month),
      Icon: CpuChipIcon,
      accent: 'text-[#d13870] bg-[#d13870]/10',
    },
    {
      key: 'storage',
      label: t.cards.storage,
      value: `${stats.storage_mb.toFixed(1)}`,
      unit: t.unit.mb,
      Icon: CircleStackIcon,
      accent: 'text-emerald-500 bg-emerald-500/10',
    },
    {
      key: 'artifacts',
      label: t.cards.artifacts,
      value: formatNumber(stats.artifact_count),
      Icon: DocumentDuplicateIcon,
      accent: 'text-gray-500 bg-gray-500/10',
    },
  ]
}

export function AdminStatsPage() {
  const query = useAdminStats()
  const stats = query.data

  const byModule = Object.entries(stats?.case_count_by_module ?? {})
  const moduleTotal = byModule.reduce((sum, [, n]) => sum + n, 0)

  return (
    <div className="max-w-5xl space-y-4">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-base font-semibold text-gray-900 dark:text-gray-100">{t.title}</h2>
          <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">{t.desc}</p>
        </div>
        <Button
          variant="secondary"
          onClick={() => void query.refetch()}
          aria-label={zh.admin.reload}
          className="!px-3"
        >
          <ArrowPathIcon
            className={cn('w-4 h-4', query.isFetching && 'animate-spin')}
            strokeWidth={1.5}
          />
        </Button>
      </header>

      {query.isPending ? (
        <div className="grid grid-cols-2 lg:grid-cols-3 gap-3">
          {[0, 1, 2, 3, 4, 5].map((i) => (
            <Skeleton key={i} shimmer className="h-24 rounded-2xl" />
          ))}
        </div>
      ) : query.isError || !stats ? (
        <Card className="p-8 text-center space-y-3">
          <p className="text-sm text-red-500">{zh.admin.loadFailed}</p>
          <Button variant="secondary" size="sm" onClick={() => void query.refetch()}>
            {zh.admin.reload}
          </Button>
        </Card>
      ) : (
        <>
          <div className="grid grid-cols-2 lg:grid-cols-3 gap-3">
            {buildTiles(stats).map(({ key, label, value, unit, note, Icon, accent }) => (
              <Card key={key} className="p-4">
                <div className="flex items-start justify-between gap-2">
                  <p className="text-xs text-gray-500 dark:text-gray-400">{label}</p>
                  <span
                    className={cn(
                      'w-8 h-8 shrink-0 rounded-lg flex items-center justify-center',
                      accent,
                    )}
                  >
                    <Icon className="w-4 h-4" strokeWidth={1.5} />
                  </span>
                </div>
                <p className="mt-2 text-2xl font-bold tabular-nums text-gray-900 dark:text-gray-100">
                  {value}
                  {unit && (
                    <span className="ml-1 text-xs font-normal text-gray-400 dark:text-gray-500">
                      {unit}
                    </span>
                  )}
                </p>
                <p className="mt-0.5 h-4 text-xs text-gray-400 dark:text-gray-500">{note ?? ''}</p>
              </Card>
            ))}
          </div>

          <Card className="p-5">
            <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100">{t.byModule}</h3>
            {byModule.length === 0 ? (
              <p className="mt-3 text-xs text-gray-400 dark:text-gray-500">{t.byModuleEmpty}</p>
            ) : (
              <div className="mt-3 space-y-2.5">
                {byModule.map(([module, count]) => {
                  const pct = moduleTotal > 0 ? Math.round((count / moduleTotal) * 100) : 0
                  return (
                    <div key={module}>
                      <div className="flex items-center justify-between text-xs">
                        <span className="text-gray-600 dark:text-gray-300">
                          {t.modules[module] ?? module}
                        </span>
                        <span className="tabular-nums text-gray-500 dark:text-gray-400">
                          {count} {t.unit.count} · {pct}%
                        </span>
                      </div>
                      <div className="mt-1 h-2 rounded-full bg-gray-100 dark:bg-gray-700 overflow-hidden">
                        <div
                          className="h-full rounded-full bg-gradient-to-r from-[#61d0e2] via-[#492497] to-[#d13870] transition-[width] duration-500"
                          style={{ width: `${Math.max(pct, count > 0 ? 4 : 0)}%` }}
                        />
                      </div>
                    </div>
                  )
                })}
              </div>
            )}
          </Card>
        </>
      )}
    </div>
  )
}
