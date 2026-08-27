import { useEffect, useId, useState } from 'react'
import { zh } from '../../i18n/zh'
import { Button } from '../../components/ui/Button'
import { Input } from '../../components/ui/Input'
import { Spinner } from '../../components/ui/Spinner'
import {
  errorMessage,
  useLlmSettings,
  useTestLlm,
  useUpdateLlm,
  type LlmSettings,
} from '../../api/settings'
import {
  ApiKeyInput,
  Field,
  SettingsCard,
  SettingsLoadError,
  SettingsSkeleton,
  TestResult,
  ToggleRow,
  type TestStatus,
} from './SettingsPrimitives'

/** 模型名 datalist 候选（标识符，非 UI 文案）。 */
const COMMON_MODELS = [
  'deepseek-chat',
  'deepseek-reasoner',
  'qwen-max',
  'qwen-plus',
  'glm-4-plus',
  'moonshot-v1-128k',
  'gpt-4o',
  'gpt-4o-mini',
  'claude-sonnet-4',
  'doubao-pro-32k',
  'ernie-4.0-turbo-8k',
]

const FALLBACK: LlmSettings = {
  base_url: '',
  api_key: '',
  model: '',
  temperature: 0.2,
  max_output_tokens: 4096,
  context_window: 131072,
  supports_json_mode: true,
}

/** 数字输入：空串保留上一有效值，避免受控 NaN。 */
function numberOr(value: string, fallback: number): number {
  const n = Number(value)
  return Number.isFinite(n) ? n : fallback
}

/** 各数值字段的取值区间（与下方 Input 的 min/max 同源）。 */
const LIMITS = {
  temperature: [0, 2],
  max_output_tokens: [256, 1_000_000],
  context_window: [4096, 10_000_000],
} as const

function clamp(value: number, [min, max]: readonly [number, number]): number {
  if (!Number.isFinite(value)) return min
  return Math.min(max, Math.max(min, value))
}

/**
 * 保存前把数值字段夹回合法区间。
 * `<input type=number>` 的 min/max 只在表单提交时校验，这里是受控输入 + 按钮直传，
 * 用户敲进去的 temperature=99 会原样 PUT 到后端；部分模型对越界温度直接报错，
 * 于是「设置里改了个数，之后所有生成都失败」，且现场看不出因果。故在此夹紧。
 */
function clampSettings(form: LlmSettings): LlmSettings {
  return {
    ...form,
    temperature: clamp(form.temperature, LIMITS.temperature),
    max_output_tokens: Math.round(clamp(form.max_output_tokens, LIMITS.max_output_tokens)),
    context_window: Math.round(clamp(form.context_window, LIMITS.context_window)),
  }
}

/** §3.7 ModelSection：GET/PUT /settings/llm + POST /settings/llm/test。 */
export function ModelSection() {
  const query = useLlmSettings()
  const update = useUpdateLlm()
  const test = useTestLlm()
  const datalistId = useId()

  const [form, setForm] = useState<LlmSettings>(FALLBACK)
  const [status, setStatus] = useState<TestStatus>({ kind: 'idle' })

  useEffect(() => {
    if (query.data) setForm(query.data)
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

  const patch = (values: Partial<LlmSettings>) => setForm((f) => ({ ...f, ...values }))

  const runTest = async () => {
    setStatus({ kind: 'idle' })
    try {
      // 用表单当前（可能未保存）的值做临时覆盖；掩码/空 api_key 由后端回落已存 key
      const result = await test.mutateAsync({
        base_url: form.base_url,
        api_key: form.api_key,
        model: form.model,
        temperature: clamp(form.temperature, LIMITS.temperature),
      })
      setStatus(
        result.ok
          ? { kind: 'ok', text: zh.settings.model.testSuccess(result.latency_ms ?? 0) }
          : { kind: 'error', text: zh.settings.model.testFailed, detail: result.error },
      )
    } catch (e) {
      setStatus({
        kind: 'error',
        text: zh.settings.model.testFailed,
        detail: errorMessage(e, zh.settings.model.testFailedNoDetail),
      })
    }
  }

  return (
    <SettingsCard
      title={zh.settings.model.title}
      description={zh.settings.model.desc}
      footer={
        <>
          <TestResult status={status} />
          <Button variant="secondary" onClick={() => void runTest()} disabled={test.isPending}>
            {test.isPending ? (
              <>
                <Spinner size="sm" />
                {zh.settings.common.testing}
              </>
            ) : (
              zh.settings.common.test
            )}
          </Button>
          <Button
            variant="primary"
            onClick={() => {
              // 夹紧结果写回表单：只静默改请求体会让输入框继续显示越界值，
              // 用户以为存的是 99，实际存的是 2。
              const safe = clampSettings(form)
              setForm(safe)
              update.mutate(safe)
            }}
            disabled={update.isPending}
          >
            {update.isPending ? zh.settings.common.saving : zh.settings.common.save}
          </Button>
        </>
      }
    >
      <Field label={zh.settings.model.baseUrl}>
        {(id) => (
          <Input
            id={id}
            value={form.base_url}
            onChange={(e) => patch({ base_url: e.target.value })}
            placeholder={zh.settings.model.baseUrlPlaceholder}
            spellCheck={false}
          />
        )}
      </Field>

      <Field label={zh.settings.model.apiKey} hint={zh.settings.common.keyKeepHint}>
        {(id) => (
          <ApiKeyInput
            id={id}
            value={form.api_key}
            onChange={(api_key) => patch({ api_key })}
            placeholder={zh.settings.model.apiKeyPlaceholder}
          />
        )}
      </Field>

      <Field label={zh.settings.model.model} hint={zh.settings.model.modelHint}>
        {(id) => (
          <>
            <Input
              id={id}
              list={datalistId}
              value={form.model}
              onChange={(e) => patch({ model: e.target.value })}
              placeholder={zh.settings.model.modelPlaceholder}
              spellCheck={false}
            />
            <datalist id={datalistId}>
              {COMMON_MODELS.map((m) => (
                <option key={m} value={m} />
              ))}
            </datalist>
          </>
        )}
      </Field>

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <Field label={zh.settings.model.temperature}>
          {(id) => (
            <Input
              id={id}
              type="number"
              min={0}
              max={2}
              step={0.1}
              value={form.temperature}
              onChange={(e) => patch({ temperature: numberOr(e.target.value, form.temperature) })}
            />
          )}
        </Field>
        <Field label={zh.settings.model.maxOutputTokens}>
          {(id) => (
            <Input
              id={id}
              type="number"
              min={256}
              step={256}
              value={form.max_output_tokens}
              onChange={(e) =>
                patch({ max_output_tokens: numberOr(e.target.value, form.max_output_tokens) })
              }
            />
          )}
        </Field>
        <Field label={zh.settings.model.contextWindow}>
          {(id) => (
            <Input
              id={id}
              type="number"
              min={4096}
              step={1024}
              value={form.context_window}
              onChange={(e) =>
                patch({ context_window: numberOr(e.target.value, form.context_window) })
              }
            />
          )}
        </Field>
      </div>
      <p className="-mt-2 text-xs text-gray-500 dark:text-gray-400">
        {zh.settings.model.temperatureHint} {zh.settings.model.contextWindowHint}
      </p>

      <ToggleRow
        title={zh.settings.model.jsonMode}
        description={zh.settings.model.jsonModeHint}
        checked={form.supports_json_mode}
        onChange={(supports_json_mode) => patch({ supports_json_mode })}
      />
    </SettingsCard>
  )
}
