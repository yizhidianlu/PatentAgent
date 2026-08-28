import { useEffect, useState } from 'react'
import { zh } from '../../i18n/zh'
import {
  emptyTier,
  errorMessage,
  useModelTiers,
  useUpdateModelTiers,
  type LlmTierSettings,
  type ModelTiersSettings,
} from '../../api/settings'
import type { ModelTier } from '../../api/sessions'
import { Button } from '../../components/ui/Button'
import { Input } from '../../components/ui/Input'
import {
  Field,
  SettingsCard,
  SettingsLoadError,
  SettingsSkeleton,
  WarningBanner,
} from './SettingsPrimitives'

const FALLBACK: ModelTiersSettings = {
  fast: emptyTier(),
  deep: emptyTier(),
  default_tier: 'deep',
}

/** 可选的正整数输入：空串表示「沿用主配置」，不是 0。 */
function optionalInt(raw: string): number | null {
  const text = raw.trim()
  if (!text) return null
  const n = Number(text)
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : null
}

function TierFields({
  title,
  value,
  effective,
  onChange,
}: {
  title: string
  value: LlmTierSettings
  effective: string
  onChange: (patch: Partial<LlmTierSettings>) => void
}) {
  return (
    <div className="rounded-xl border border-gray-200/60 dark:border-gray-700 p-4 space-y-3">
      <div className="flex items-baseline justify-between gap-2">
        <h4 className="text-sm font-medium text-gray-900 dark:text-gray-100">{title}</h4>
        {effective && (
          <span className="text-[11px] text-gray-400 truncate">
            {zh.settings.tiers.effective(effective)}
          </span>
        )}
      </div>
      <Field label={zh.settings.tiers.modelLabel}>
        {(id) => (
          <Input
            id={id}
            value={value.model}
            placeholder={zh.settings.tiers.modelPlaceholder}
            onChange={(e) => onChange({ model: e.target.value })}
          />
        )}
      </Field>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label={zh.settings.tiers.labelLabel}>
          {(id) => (
            <Input
              id={id}
              value={value.label}
              placeholder={zh.settings.tiers.labelPlaceholder}
              onChange={(e) => onChange({ label: e.target.value })}
            />
          )}
        </Field>
        <Field label={zh.settings.tiers.maxOutputLabel}>
          {(id) => (
            <Input
              id={id}
              inputMode="numeric"
              value={value.max_output_tokens === null ? '' : String(value.max_output_tokens)}
              placeholder={zh.settings.tiers.inheritPlaceholder}
              onChange={(e) => onChange({ max_output_tokens: optionalInt(e.target.value) })}
            />
          )}
        </Field>
      </div>
    </div>
  )
}

/**
 * 两档模型的配置卡。
 *
 * 只给「模型名 + 显示名 + 最大输出」三项，**不给服务地址与密钥**：
 * 档位回答的是「这次要快还是要想清楚」，不是「换一家供应商」。
 * 每多一个密钥输入框就多一个密钥外发的入口，那类问题这个项目已经踩过一次。
 */
export function TiersSection() {
  const query = useModelTiers()
  const update = useUpdateModelTiers()
  const [form, setForm] = useState<ModelTiersSettings>(FALLBACK)

  useEffect(() => {
    if (query.data) {
      setForm({
        fast: query.data.fast,
        deep: query.data.deep,
        default_tier: query.data.default_tier,
      })
    }
  }, [query.data])

  if (query.isLoading) return <SettingsSkeleton />
  if (query.isError) {
    return (
      <SettingsLoadError
        onRetry={() => void query.refetch()}
        detail={errorMessage(query.error, '')}
      />
    )
  }

  const base = query.data?.base_model ?? ''
  const effectiveOf = (tier: ModelTier): string => form[tier].model.trim() || base
  // 两档指向同一个模型时聊天框上的开关不会渲染——这里必须说出来，
  // 否则用户会以为自己配好了，回到聊天框却找不到开关
  const sameModel = Boolean(effectiveOf('fast')) && effectiveOf('fast') === effectiveOf('deep')

  return (
    <SettingsCard
      title={zh.settings.tiers.title}
      description={zh.settings.tiers.desc}
      footer={
        <Button
          variant="primary"
          onClick={() => update.mutate(form)}
          disabled={update.isPending}
        >
          {update.isPending ? zh.settings.common.saving : zh.settings.common.save}
        </Button>
      }
    >
      <p className="text-xs text-gray-500 dark:text-gray-400">
        {zh.settings.tiers.onlyModelHint}
      </p>

      {sameModel && <WarningBanner>{zh.settings.tiers.sameModelWarning}</WarningBanner>}

      <div className="grid gap-4 lg:grid-cols-2">
        <TierFields
          title={zh.settings.tiers.fastTitle}
          value={form.fast}
          effective={effectiveOf('fast')}
          onChange={(patch) => setForm((f) => ({ ...f, fast: { ...f.fast, ...patch } }))}
        />
        <TierFields
          title={zh.settings.tiers.deepTitle}
          value={form.deep}
          effective={effectiveOf('deep')}
          onChange={(patch) => setForm((f) => ({ ...f, deep: { ...f.deep, ...patch } }))}
        />
      </div>

      <Field label={zh.settings.tiers.defaultTier}>
        {() => (
        <div className="inline-flex rounded-full bg-gray-100 dark:bg-gray-800 p-0.5">
          {(['fast', 'deep'] as const).map((tier) => (
            <button
              key={tier}
              type="button"
              onClick={() => setForm((f) => ({ ...f, default_tier: tier }))}
              className={
                'rounded-full px-3 py-1 text-xs transition-colors ' +
                (form.default_tier === tier
                  ? 'bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-50 shadow-sm'
                  : 'text-gray-500 hover:text-gray-700 dark:hover:text-gray-300')
              }
            >
              {tier === 'fast' ? zh.tier.fast : zh.tier.deep}
            </button>
          ))}
        </div>
        )}
      </Field>
    </SettingsCard>
  )
}
