import { useEffect, useId, useState } from 'react'
import { zh } from '../../i18n/zh'
import { Button } from '../../components/ui/Button'
import { Input } from '../../components/ui/Input'
import { Select } from '../../components/ui/Select'
import { Spinner } from '../../components/ui/Spinner'
import {
  errorMessage,
  useImageGenSettings,
  useTestImageGen,
  useUpdateImageGen,
  type ImageGenProvider,
  type ImageGenSettings,
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

const PROVIDERS: ImageGenProvider[] = ['zhipu', 'dashscope', 'openai', 'local', 'custom']

/** 常见出图尺寸（标识符，非 UI 文案）。 */
const COMMON_SIZES = ['512x512', '768x768', '1024x1024', '1024x1792', '1792x1024']

const FALLBACK: ImageGenSettings = {
  enabled: false,
  provider: 'custom',
  base_url: '',
  api_key: '',
  model: '',
  size: '1024x1024',
}

/** §3.7 ImageGenSection：附图/线稿图像模型配置 + 测试出图（缩略图 w-24）。 */
export function ImageGenSection() {
  const query = useImageGenSettings()
  const update = useUpdateImageGen()
  const test = useTestImageGen()
  const sizeListId = useId()

  const [form, setForm] = useState<ImageGenSettings>(FALLBACK)
  const [status, setStatus] = useState<TestStatus>({ kind: 'idle' })
  const [preview, setPreview] = useState<string | null>(null)

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

  const patch = (values: Partial<ImageGenSettings>) => setForm((f) => ({ ...f, ...values }))

  const runTest = async () => {
    setStatus({ kind: 'idle' })
    setPreview(null)
    try {
      const result = await test.mutateAsync({
        base_url: form.base_url,
        api_key: form.api_key,
        model: form.model,
        size: form.size,
      })
      if (result.ok) {
        setStatus({ kind: 'ok', text: zh.settings.imageGen.testSuccess(result.latency_ms ?? 0) })
        setPreview(result.image_data_url ?? result.image_url)
      } else {
        setStatus({ kind: 'error', text: zh.settings.imageGen.testFailed, detail: result.error })
      }
    } catch (e) {
      setStatus({
        kind: 'error',
        text: zh.settings.imageGen.testFailed,
        detail: errorMessage(e, zh.settings.imageGen.testFailed),
      })
    }
  }

  return (
    <SettingsCard
      title={zh.settings.imageGen.title}
      description={zh.settings.imageGen.desc}
      footer={
        <>
          <TestResult status={status} />
          <Button variant="secondary" onClick={() => void runTest()} disabled={test.isPending}>
            {test.isPending ? (
              <>
                <Spinner size="sm" />
                {zh.settings.imageGen.testing}
              </>
            ) : (
              zh.settings.imageGen.test
            )}
          </Button>
          <Button variant="primary" onClick={() => update.mutate(form)} disabled={update.isPending}>
            {update.isPending ? zh.settings.common.saving : zh.settings.common.save}
          </Button>
        </>
      }
    >
      <ToggleRow
        title={zh.settings.imageGen.enable}
        description={zh.settings.imageGen.enableDesc}
        checked={form.enabled}
        onChange={(enabled) => patch({ enabled })}
      />

      <Field label={zh.settings.imageGen.provider}>
        {(id) => (
          <Select
            id={id}
            value={form.provider}
            onChange={(e) => patch({ provider: e.target.value as ImageGenProvider })}
          >
            {PROVIDERS.map((p) => (
              <option key={p} value={p}>
                {zh.settings.imageGen.providers[p]}
              </option>
            ))}
          </Select>
        )}
      </Field>

      <Field label={zh.settings.imageGen.baseUrl}>
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

      <Field label={zh.settings.imageGen.apiKey} hint={zh.settings.common.keyKeepHint}>
        {(id) => (
          <ApiKeyInput
            id={id}
            value={form.api_key}
            onChange={(api_key) => patch({ api_key })}
            placeholder={zh.settings.model.apiKeyPlaceholder}
          />
        )}
      </Field>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <Field label={zh.settings.imageGen.model}>
          {(id) => (
            <Input
              id={id}
              value={form.model}
              onChange={(e) => patch({ model: e.target.value })}
              placeholder={zh.settings.imageGen.modelPlaceholder}
              spellCheck={false}
            />
          )}
        </Field>
        <Field label={zh.settings.imageGen.size}>
          {(id) => (
            <>
              <Input
                id={id}
                list={sizeListId}
                value={form.size}
                onChange={(e) => patch({ size: e.target.value })}
                spellCheck={false}
              />
              <datalist id={sizeListId}>
                {COMMON_SIZES.map((s) => (
                  <option key={s} value={s} />
                ))}
              </datalist>
            </>
          )}
        </Field>
      </div>

      {preview && (
        <div className="flex items-center gap-3">
          <img
            src={preview}
            alt={zh.settings.imageGen.previewAlt}
            className="w-24 h-24 rounded-xl border border-gray-200 dark:border-gray-700 object-contain bg-white"
          />
          <a
            href={preview}
            target="_blank"
            rel="noreferrer"
            className="text-xs text-[#6366f1] hover:underline"
          >
            {zh.settings.imageGen.previewLink}
          </a>
        </div>
      )}

      <p className="text-xs text-gray-500 dark:text-gray-400">{zh.settings.imageGen.usageNote}</p>
    </SettingsCard>
  )
}
