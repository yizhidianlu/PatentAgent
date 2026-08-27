import { useState } from 'react'
import { Link } from 'react-router-dom'
import {
  ArrowPathIcon,
  ArrowTopRightOnSquareIcon,
  ChevronDownIcon,
  Cog6ToothIcon,
  LockClosedIcon,
} from '@heroicons/react/24/outline'
import { useSkills, useToggleSkill, type Skill } from '../api/skills'
import { useAuthStore } from '../stores/authStore'
import { useUiStore } from '../stores/uiStore'
import { Card } from '../components/ui/Card'
import { ToggleSwitch } from '../components/ui/ToggleSwitch'
import { Skeleton } from '../components/ui/Skeleton'
import { cn } from '../lib/cn'
import { zh } from '../i18n/zh'

const t = zh.skills

/** 状态点：用一个小圆点而不是徽章——它是辅助信息，不该和技能名争视线。 */
function StatusDot({ status }: { status: Skill['status'] }) {
  const cls =
    status === 'available'
      ? 'bg-emerald-500'
      : status === 'needs_config'
        ? 'bg-amber-400'
        : 'bg-gray-300 dark:bg-gray-600'
  return (
    <span
      className={cn('inline-block w-1.5 h-1.5 rounded-full shrink-0', cls)}
      title={t.statuses[status] ?? status}
      aria-label={t.statuses[status] ?? status}
    />
  )
}

/**
 * 一项技能。
 *
 * 卡片正面只留「名称 + 一句话 + 开关」三样。适用范围、输入输出、开销、来源
 * 全部收进折叠区——它们是决定要不要开时才看的，平铺出来会让整页密不透风。
 */
function SkillRow({
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
  const switchDisabled = !canToggle || !skill.toggleable || busy || unmet.length > 0

  return (
    <div className="border-b border-gray-100 dark:border-gray-700/50 last:border-0">
      <div className="flex items-center gap-3 py-3">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="min-w-0 flex-1 flex items-center gap-2 text-left group"
        >
          <StatusDot status={skill.status} />
          <span className="min-w-0">
            <span className="flex items-center gap-1.5">
              <span className="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">
                {skill.name}
              </span>
              {!skill.toggleable && (
                <LockClosedIcon
                  className="w-3 h-3 shrink-0 text-gray-300 dark:text-gray-600"
                  strokeWidth={1.5}
                  title={t.alwaysOnHint}
                />
              )}
            </span>
            <span className="block mt-0.5 text-xs text-gray-500 dark:text-gray-400 truncate">
              {skill.summary}
            </span>
          </span>
          <ChevronDownIcon
            className={cn(
              'w-3.5 h-3.5 shrink-0 text-gray-300 dark:text-gray-600 transition-transform',
              'group-hover:text-gray-400',
              open && 'rotate-180',
            )}
            strokeWidth={2}
          />
        </button>

        <ToggleSwitch
          checked={skill.enabled}
          onChange={onToggle}
          disabled={switchDisabled}
          label={skill.name}
        />
      </div>

      {open && (
        <div className="pb-3.5 pl-[18px] space-y-2.5 text-xs">
          {skill.description && (
            <p className="whitespace-pre-line leading-relaxed text-gray-600 dark:text-gray-300">
              {skill.description}
            </p>
          )}

          <dl className="grid gap-x-6 gap-y-1.5 sm:grid-cols-2 text-gray-600 dark:text-gray-300">
            <div>
              <dt className="text-gray-400 dark:text-gray-500">{t.appliesTo}</dt>
              <dd>
                {skill.modules.map((m) => t.modules[m] ?? m).join('、')}
                {skill.patent_types.length > 0 && skill.patent_types.length < 3 && (
                  <span className="text-gray-400">
                    （{skill.patent_types.map((p) => t.patentTypes[p] ?? p).join(' / ')}）
                  </span>
                )}
              </dd>
            </div>
            {skill.cost_hint && (
              <div>
                <dt className="text-gray-400 dark:text-gray-500">{t.costHint}</dt>
                <dd>{skill.cost_hint}</dd>
              </div>
            )}
            {skill.inputs && (
              <div>
                <dt className="text-gray-400 dark:text-gray-500">{t.inputs}</dt>
                <dd>{skill.inputs}</dd>
              </div>
            )}
            {skill.outputs && (
              <div>
                <dt className="text-gray-400 dark:text-gray-500">{t.outputs}</dt>
                <dd>{skill.outputs}</dd>
              </div>
            )}
          </dl>

          {/* 外部来源如实标注：既是许可证义务，也让用户知道这段能力从哪来 */}
          {skill.provider !== 'builtin' && (
            <p className="flex flex-wrap items-center gap-x-3 text-[11px] text-gray-400 dark:text-gray-500">
              <span>
                {t.source}：{skill.provider}
                {skill.license && ` · ${skill.license}`}
              </span>
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
            </p>
          )}
        </div>
      )}

      {/* 缺前置条件时始终显示——这是「为什么开关是灰的」的答案，藏进折叠里等于没说 */}
      {unmet.length > 0 && (
        <div className="pb-3 pl-[18px] space-y-1">
          {unmet.map((r) => (
            <p key={r.key} className="text-[11px] text-gray-500 dark:text-gray-400">
              <span className="text-amber-600 dark:text-amber-400">{r.label}</span>
              {r.hint && <span className="ml-1">{r.hint}</span>}
              {r.settings_path && (
                <Link
                  to={r.settings_path}
                  className="ml-1 inline-flex items-center gap-0.5 text-[#492497] dark:text-[#61d0e2] hover:underline"
                >
                  <Cog6ToothIcon className="w-3 h-3" strokeWidth={1.5} />
                  {t.goConfigure}
                </Link>
              )}
            </p>
          ))}
        </div>
      )}
    </div>
  )
}

/** §技能库：平台支持的各项能力，按分类分组，可开关。 */
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
        onSuccess: () =>
          pushToast('success', next ? t.enabledOk(skill.name) : t.disabledOk(skill.name)),
        onError: () => pushToast('error', t.toggleFailed),
        onSettled: () => setBusyKey(null),
      },
    )
  }

  return (
    // max-w-4xl mx-auto：与案例库一致。此前没有宽度约束，宽屏上卡片会拉满整行，
    // 一句话摘要孤零零躺在两千像素里，看着又空又乱。
    <div className="max-w-4xl mx-auto p-4 sm:p-8 space-y-6">
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
          {[0, 1].map((i) => (
            <Skeleton key={i} className="h-40 rounded-2xl" />
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

      {/* 一个分类一张卡，技能在卡内以行排列——而不是每项技能各占一张卡。
          后者在只有六七项能力时会把页面撑得很散。 */}
      {query.data?.categories.map((cat) => {
        const items = query.data.skills.filter((s) => s.category === cat.key)
        if (items.length === 0) return null
        const onCount = items.filter((s) => s.enabled && s.status === 'available').length
        return (
          <Card key={cat.key} className="px-4 py-1">
            <div className="flex items-center justify-between gap-2 pt-2.5 pb-1">
              <h3 className="text-xs font-medium text-gray-500 dark:text-gray-400">{cat.label}</h3>
              <span className="text-[11px] tabular-nums text-gray-400 dark:text-gray-500">
                {onCount}/{items.length}
              </span>
            </div>
            {items.map((s) => (
              <SkillRow
                key={s.key}
                skill={s}
                canToggle={isAdmin}
                busy={busyKey === s.key}
                onToggle={(next) => handleToggle(s, next)}
              />
            ))}
          </Card>
        )
      })}

      {query.data && query.data.skills.length === 0 && (
        <Card className="p-8 text-center text-sm text-gray-500 dark:text-gray-400">{t.empty}</Card>
      )}
    </div>
  )
}
