import { useEffect, useState } from 'react'
import { zh } from '../../i18n/zh'
import {
  emptyTier,
  errorMessage,
  useModelTiers,
  useTestModelTier,
  useUpdateModelTiers,
  type LlmTierSettings,
  type ModelTiersSettings,
} from '../../api/settings'
import type { ModelTier } from '../../api/sessions'
import { Button } from '../../components/ui/Button'
import { Input } from '../../components/ui/Input'
import {
  ApiKeyInput,
  Field,
  SettingsCard,
  SettingsLoadError,
  SettingsSkeleton,
  TestResult,
  WarningBanner,
  type TestStatus,
} from './SettingsPrimitives'
import { Spinner } from '../../components/ui/Spinner'

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


/** 小型分段选择：思考模式 / 推理强度共用。 */
function Segmented<T extends string>({
  value,
  options,
  onChange,
}: {
  value: T
  options: { key: T; label: string }[]
  onChange: (next: T) => void
}) {
  return (
    <div className="inline-flex rounded-full bg-gray-100 dark:bg-gray-800 p-0.5">
      {options.map((o) => (
        <button
          key={o.key}
          type="button"
          onClick={() => onChange(o.key)}
          className={
            'rounded-full px-2.5 py-1 text-xs transition-colors ' +
            (value === o.key
              ? 'bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-50 shadow-sm'
              : 'text-gray-500 hover:text-gray-700 dark:hover:text-gray-300')
          }
        >
          {o.label}
        </button>
      ))}
    </div>
  )
}

function TierFields({
  title,
  value,
  effective,
  effectiveUrl,
  onChange,
  onTest,
  testing,
  status,
}: {
  title: string
  value: LlmTierSettings
  effective: string
  effectiveUrl: string
  onChange: (patch: Partial<LlmTierSettings>) => void
  onTest: () => void
  testing: boolean
  status: TestStatus
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
      <p className="text-[11px] text-gray-400 truncate">
        {zh.settings.tiers.effectiveAt(effectiveUrl)}
      </p>
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

      {/* 「快档」真正的开关：显式声明要不要思考，而不是赌模型名 */}
      <Field label={zh.settings.tiers.thinkingLabel} hint={zh.settings.tiers.thinkingHint}>
        {() => (
          <div className="flex items-center gap-2 flex-wrap">
            <Segmented
              value={value.thinking}
              onChange={(v) => onChange({ thinking: v })}
              options={[
                { key: 'auto', label: zh.settings.tiers.thinkingAuto },
                { key: 'enabled', label: zh.settings.tiers.thinkingOn },
                { key: 'disabled', label: zh.settings.tiers.thinkingOff },
              ]}
            />
            <span className="text-[11px] text-gray-400">
              {zh.settings.tiers.effortLabel}
            </span>
            <Segmented
              value={value.reasoning_effort}
              onChange={(v) => onChange({ reasoning_effort: v })}
              options={[
                { key: 'auto', label: zh.settings.tiers.effortAuto },
                { key: 'low', label: 'low' },
                { key: 'high', label: 'high' },
              ]}
            />
          </div>
        )}
      </Field>

      {/* 跨供应商分档：地址与密钥都留空时，这一档完全跟随主配置 */}
      <Field label={zh.settings.tiers.baseUrlLabel}>
        {(id) => (
          <Input
            id={id}
            value={value.base_url}
            placeholder={zh.settings.tiers.baseUrlPlaceholder}
            onChange={(e) => onChange({ base_url: e.target.value })}
          />
        )}
      </Field>
      <Field label={zh.settings.tiers.apiKeyLabel}>
        {(id) => (
          <ApiKeyInput
            id={id}
            value={value.api_key}
            placeholder={zh.settings.tiers.apiKeyPlaceholder}
            onChange={(v) => onChange({ api_key: v })}
          />
        )}
      </Field>

      <div className="flex items-center gap-2 flex-wrap">
        <Button variant="secondary" size="sm" onClick={onTest} disabled={testing}>
          {testing ? (
            <>
              <Spinner size="sm" />
              {zh.settings.tiers.testing}
            </>
          ) : (
            zh.settings.tiers.test
          )}
        </Button>
        <span className="text-[11px] text-gray-400">{zh.settings.tiers.testHint}</span>
      </div>
      <TestResult status={status} />
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
  const test = useTestModelTier()
  const [form, setForm] = useState<ModelTiersSettings>(FALLBACK)
  const [status, setStatus] = useState<Record<ModelTier, TestStatus>>({
    fast: { kind: 'idle' },
    deep: { kind: 'idle' },
  })
  const [testingTier, setTestingTier] = useState<ModelTier | null>(null)

  const runTest = async (tier: ModelTier) => {
    setTestingTier(tier)
    setStatus((s) => ({ ...s, [tier]: { kind: 'idle' } }))
    try {
      const result = await test.mutateAsync(tier)
      // 把「实际打到哪儿」一并显示出来：模型名是服务端回声，一个配错的中转
      // 也能回出你想看的名字；两档指向不同供应商时，地址是唯一能自查的东西
      const hit = result.target_base_url
        ? zh.settings.tiers.testHitAt(result.target_base_url)
        : undefined
      setStatus((s) => ({
        ...s,
        [tier]: result.ok
          ? { kind: 'ok', text: zh.settings.tiers.testOk(result.latency_ms ?? 0), detail: hit }
          : {
              kind: 'error',
              text: zh.settings.tiers.testFailed,
              detail: [hit, result.error].filter(Boolean).join('　'),
            },
      }))
    } catch (e) {
      setStatus((s) => ({
        ...s,
        [tier]: {
          kind: 'error',
          text: zh.settings.tiers.testFailed,
          detail: errorMessage(e, ''),
        },
      }))
    } finally {
      setTestingTier(null)
    }
  }

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
  const urlOf = (tier: ModelTier): string =>
    form[tier].base_url.trim() || (query.data?.base_url ?? '')
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
      <p className="text-xs text-gray-500 dark:text-gray-400">
        {zh.settings.tiers.crossProviderNote}
      </p>

      {sameModel && <WarningBanner>{zh.settings.tiers.sameModelWarning}</WarningBanner>}

      <div className="grid gap-4 lg:grid-cols-2">
        <TierFields
          title={zh.settings.tiers.fastTitle}
          value={form.fast}
          effective={effectiveOf('fast')}
          effectiveUrl={urlOf('fast')}
          onChange={(patch) => setForm((f) => ({ ...f, fast: { ...f.fast, ...patch } }))}
          onTest={() => void runTest('fast')}
          testing={testingTier === 'fast'}
          status={status.fast}
        />
        <TierFields
          title={zh.settings.tiers.deepTitle}
          value={form.deep}
          effective={effectiveOf('deep')}
          effectiveUrl={urlOf('deep')}
          onChange={(patch) => setForm((f) => ({ ...f, deep: { ...f.deep, ...patch } }))}
          onTest={() => void runTest('deep')}
          testing={testingTier === 'deep'}
          status={status.deep}
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
