import { useState } from 'react'
import { Link } from 'react-router-dom'
import {
  ArrowPathIcon,
  ArrowTopRightOnSquareIcon,
  ChevronDownIcon,
  CheckCircleIcon,
  Cog6ToothIcon,
  ExclamationTriangleIcon,
  LockClosedIcon,
} from '@heroicons/react/24/outline'
import { useSkills, useToggleSkill, type Skill } from '../api/skills'
import { useAuthStore } from '../stores/authStore'
import { useUiStore } from '../stores/uiStore'
import { Badge } from '../components/ui/Badge'
import { Card } from '../components/ui/Card'
import { ToggleSwitch } from '../components/ui/ToggleSwitch'
import { Skeleton } from '../components/ui/Skeleton'
import { cn } from '../lib/cn'
import { zh } from '../i18n/zh'

const t = zh.skills

function StatusBadge({ status }: { status: Skill['status'] }) {
  const variant =
    status === 'available' ? 'emerald' : status === 'needs_config' ? 'amber' : 'neutral'
  return <Badge variant={variant}>{t.statuses[status] ?? status}</Badge>
}

/** 一项技能的卡片：摘要 + 适用范围 + 开关，点击展开详情。 */
function SkillCard({
  skill,
  canToggle,
  onToggle,
  busy,
}: {
  skill: Skill
  canToggle: boolean
  onToggle: (next: boolean) => void
  busy: boolean
}) {
  const [open, setOpen] = useState(false)
  const unmet = skill.requirements.filter((r) => !r.satisfied)
  // 前置条件没满足时开关是灰的：让用户点开一个必然不生效的技能，
  // 只会让他以为开了就有用，然后困惑于为什么没反应
  const switchDisabled = !canToggle || !skill.toggleable || busy || unmet.length > 0

  return (
    <Card className="p-0 overflow-hidden">
      <div className="flex items-start gap-3 px-4 py-3.5">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100">{skill.name}</h3>
            <StatusBadge status={skill.status} />
            {!skill.toggleable && (
              <span
                className="inline-flex items-center gap-1 text-[11px] text-gray-400 dark:text-gray-500"
                title={t.alwaysOnHint}
              >
                <LockClosedIcon className="w-3 h-3" strokeWidth={1.5} />
                {t.alwaysOn}
              </span>
            )}
          </div>
          <p className="mt-1 text-xs leading-relaxed text-gray-600 dark:text-gray-300">
            {skill.summary}
          </p>

          <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[11px] text-gray-400 dark:text-gray-500">
            <span>{t.appliesTo}</span>
            {skill.modules.map((m) => (
              <span
                key={m}
                className="rounded bg-gray-100 dark:bg-gray-700/60 px-1.5 py-0.5 text-gray-500 dark:text-gray-300"
              >
                {t.modules[m] ?? m}
              </span>
            ))}
            {skill.patent_types.length > 0 && skill.patent_types.length < 3 && (
              <span className="text-gray-400">
                · {skill.patent_types.map((p) => t.patentTypes[p] ?? p).join(' / ')}
              </span>
            )}
          </div>

          {skill.cost_hint && (
            <p className="mt-1.5 text-[11px] text-amber-600 dark:text-amber-400">
              {t.costHint}：{skill.cost_hint}
            </p>
          )}
        </div>

        <div className="shrink-0 flex flex-col items-end gap-2">
          <ToggleSwitch
            checked={skill.enabled}
            onChange={onToggle}
            disabled={switchDisabled}
            label={skill.name}
          />
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            className="inline-flex items-center gap-0.5 text-[11px] text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
          >
            <ChevronDownIcon
              className={cn('w-3.5 h-3.5 transition-transform', open && 'rotate-180')}
              strokeWidth={2}
            />
          </button>
        </div>
      </div>

      {/* 未满足的前置条件始终显示——它是「为什么用不了」的答案，不该藏在折叠里 */}
      {unmet.length > 0 && (
        <div className="border-t border-gray-100 dark:border-gray-700/60 bg-amber-50/50 dark:bg-amber-500/5 px-4 py-2.5 space-y-1.5">
          {unmet.map((r) => (
            <div key={r.key} className="flex items-start gap-2 text-[11px]">
              <ExclamationTriangleIcon
                className="w-3.5 h-3.5 shrink-0 mt-px text-amber-500"
                strokeWidth={1.5}
              />
              <div className="min-w-0 flex-1">
                <span className="text-gray-700 dark:text-gray-200">{r.label}</span>
                {r.hint && (
                  <span className="ml-1 text-gray-500 dark:text-gray-400">{r.hint}</span>
                )}
              </div>
              {r.settings_path && (
                <Link
                  to={r.settings_path}
                  className="shrink-0 inline-flex items-center gap-0.5 text-[#492497] dark:text-[#61d0e2] hover:underline"
                >
                  <Cog6ToothIcon className="w-3 h-3" strokeWidth={1.5} />
                  {t.goConfigure}
                </Link>
              )}
            </div>
          ))}
        </div>
      )}

      {open && (
        <div className="border-t border-gray-100 dark:border-gray-700/60 px-4 py-3 space-y-2.5 text-xs">
          {skill.description && (
            <p className="whitespace-pre-line leading-relaxed text-gray-600 dark:text-gray-300">
              {skill.description}
            </p>
          )}
          <dl className="grid gap-1.5 sm:grid-cols-2">
            {skill.inputs && (
              <div>
                <dt className="text-[11px] text-gray-400">{t.inputs}</dt>
                <dd className="text-gray-700 dark:text-gray-200">{skill.inputs}</dd>
              </div>
            )}
            {skill.outputs && (
              <div>
                <dt className="text-[11px] text-gray-400">{t.outputs}</dt>
                <dd className="text-gray-700 dark:text-gray-200">{skill.outputs}</dd>
              </div>
            )}
          </dl>
          {/* 外部来源必须如实标注：既是许可证义务，也让用户知道这段能力来自哪里 */}
          {skill.provider !== 'builtin' && (
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 pt-1 text-[11px] text-gray-400 dark:text-gray-500">
              <span>
                {t.source}：{skill.provider}
              </span>
              {skill.license && (
                <span>
                  {t.license}：{skill.license}
                </span>
              )}
              {skill.source_url && (
                <a
                  href={skill.source_url}
                  target="_blank"
                  rel="noreferrer noopener"
                  className="inline-flex items-center gap-0.5 text-[#492497] dark:text-[#61d0e2] hover:underline"
                >
                  {skill.source_url.replace(/^https?:\/\//, '')}
                  <ArrowTopRightOnSquareIcon className="w-3 h-3" strokeWidth={1.5} />
                </a>
              )}
            </div>
          )}
        </div>
      )}
    </Card>
  )
}

/** §技能库：平台支持的各项能力，按分类分组展示，可开关。 */
export function SkillsPage() {
  const query = useSkills()
  const toggle = useToggleSkill()
  const isAdmin = useAuthStore((s) => s.user?.role === 'admin')
  const pushToast = useUiStore((s) => s.pushToast)
  const [busyKey, setBusyKey] = useState<string | null>(null)

  const handleToggle = (skill: Skill, next: boolean) => {
    if (!isAdmin) {
      pushToast('info', t.adminOnly)
      return
    }
    setBusyKey(skill.key)
    toggle.mutate(
      { key: skill.key, enabled: next },
      {
        onSuccess: () => pushToast('success', next ? t.enabledOk(skill.name) : t.disabledOk(skill.name)),
        onError: () => pushToast('error', t.toggleFailed),
        onSettled: () => setBusyKey(null),
      },
    )
  }

  return (
    <div className="space-y-5">
      <header className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-base font-semibold text-gray-900 dark:text-gray-100">{t.title}</h2>
          <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">{t.desc}</p>
        </div>
        <button
          type="button"
          onClick={() => void query.refetch()}
          disabled={query.isFetching}
          className="shrink-0 w-8 h-8 rounded-lg flex items-center justify-center text-gray-400 hover:text-gray-600 hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-50 transition-colors"
          aria-label={t.retry}
        >
          <ArrowPathIcon
            className={cn('w-4 h-4', query.isFetching && 'animate-spin')}
            strokeWidth={1.5}
          />
        </button>
      </header>

      {query.isLoading && (
        <div className="space-y-3">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-24 rounded-2xl" />
          ))}
        </div>
      )}

      {query.isError && (
        <Card className="p-6 text-center space-y-2">
          <p className="text-sm text-gray-600 dark:text-gray-300">{t.loadFailed}</p>
          <button
            type="button"
            onClick={() => void query.refetch()}
            className="text-xs text-[#492497] dark:text-[#61d0e2] hover:underline"
          >
            {t.retry}
          </button>
        </Card>
      )}

      {query.data?.categories.map((cat) => {
        const items = query.data.skills.filter((s) => s.category === cat.key)
        if (items.length === 0) return null
        return (
          <section key={cat.key} className="space-y-2.5">
            <h3 className="flex items-center gap-2 text-xs font-medium text-gray-500 dark:text-gray-400">
              {cat.label}
              <span className="text-gray-300 dark:text-gray-600">·</span>
              <span className="inline-flex items-center gap-1 text-gray-400">
                <CheckCircleIcon className="w-3.5 h-3.5 text-emerald-500" strokeWidth={1.5} />
                {items.filter((s) => s.enabled && s.status === 'available').length}/{items.length}
              </span>
            </h3>
            <div className="space-y-2.5">
              {items.map((s) => (
                <SkillCard
                  key={s.key}
                  skill={s}
                  canToggle={isAdmin}
                  busy={busyKey === s.key}
                  onToggle={(next) => handleToggle(s, next)}
                />
              ))}
            </div>
          </section>
        )
      })}

      {query.data && query.data.skills.length === 0 && (
        <Card className="p-8 text-center text-sm text-gray-500 dark:text-gray-400">{t.empty}</Card>
      )}
    </div>
  )
}
