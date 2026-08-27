import { useEffect, useState } from 'react'
import { zh } from '../../i18n/zh'
import { Button } from '../../components/ui/Button'
import { Input } from '../../components/ui/Input'
import { Modal } from '../../components/ui/Modal'
import { Select } from '../../components/ui/Select'
import { Spinner } from '../../components/ui/Spinner'
import {
  errorMessage,
  useEmbeddingSettings,
  useReindexEmbedding,
  useTestEmbedding,
  useUpdateEmbedding,
  type EmbeddingProvider,
  type EmbeddingSettings,
} from '../../api/settings'
import {
  ApiKeyInput,
  Field,
  SettingsCard,
  SettingsLoadError,
  SettingsSkeleton,
  TestResult,
  ToggleRow,
  WarningBanner,
  type TestStatus,
} from './SettingsPrimitives'

const PROVIDERS: EmbeddingProvider[] = ['zhipu', 'dashscope', 'minimax', 'local', 'custom']

/** 向量维度合法区间（与下方 Input 的 min/max 同源）。 */
const MIN_DIM = 64
const MAX_DIM = 8192

const FALLBACK: EmbeddingSettings = {
  enabled: false,
  provider: 'custom',
  base_url: '',
  api_key: '',
  model: '',
  dim: 1024,
}

/** §3.7 EmbeddingSection：启用开关 + 服务商 + 连接测试 + 重建索引。 */
export function EmbeddingSection() {
  const query = useEmbeddingSettings()
  const update = useUpdateEmbedding()
  const test = useTestEmbedding()
  const reindex = useReindexEmbedding()

  const [form, setForm] = useState<EmbeddingSettings>(FALLBACK)
  const [status, setStatus] = useState<TestStatus>({ kind: 'idle' })
  const [dimMismatch, setDimMismatch] = useState<number | null>(null)
  const [needRebuild, setNeedRebuild] = useState(false)
  const [confirmOpen, setConfirmOpen] = useState(false)

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

  const saved = query.data ?? FALLBACK
  const patch = (values: Partial<EmbeddingSettings>) => setForm((f) => ({ ...f, ...values }))
  // 维度与已存值不同（尚未保存），或上次保存后端回了 need_rebuild
  const dimDirty = form.dim !== saved.dim
  const showRebuildBanner = needRebuild || dimDirty

  const runTest = async () => {
    setStatus({ kind: 'idle' })
    setDimMismatch(null)
    try {
      const result = await test.mutateAsync({
        base_url: form.base_url,
        api_key: form.api_key,
        model: form.model,
      })
      if (result.ok) {
        setStatus({
          kind: 'ok',
          text: zh.settings.embedding.testSuccess(result.dim ?? 0, result.latency_ms ?? 0),
        })
        if (result.dim && result.dim !== form.dim) setDimMismatch(result.dim)
      } else {
        setStatus({ kind: 'error', text: zh.settings.embedding.testFailed, detail: result.error })
      }
    } catch (e) {
      setStatus({
        kind: 'error',
        text: zh.settings.embedding.testFailed,
        detail: errorMessage(e, zh.settings.embedding.testFailed),
      })
    }
  }

  const save = async () => {
    // dim 与 Input 的 min/max 同源夹紧：受控输入不走表单校验，
    // 负数 / 0 维度会一路 PUT 到后端，之后整个向量库按错误维度重建。
    const safe: EmbeddingSettings = {
      ...form,
      dim: Math.round(Math.min(MAX_DIM, Math.max(MIN_DIM, Number(form.dim) || MIN_DIM))),
    }
    setForm(safe)
    const result = await update.mutateAsync(safe).catch(() => null)
    if (result) setNeedRebuild(result.need_rebuild)
  }

  const runReindex = async () => {
    setConfirmOpen(false)
    const result = await reindex.mutateAsync().catch(() => null)
    if (result) setNeedRebuild(false)
  }

  return (
    <div className="space-y-4">
      {showRebuildBanner && <WarningBanner>{zh.settings.embedding.dimChanged}</WarningBanner>}

      <SettingsCard
        title={zh.settings.embedding.title}
        description={zh.settings.embedding.desc}
        footer={
          <>
            <TestResult status={status} />
            <Button
              variant="ghost"
              onClick={() => setConfirmOpen(true)}
              disabled={reindex.isPending}
              className="text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-500/10"
            >
              {reindex.isPending ? (
                <>
                  <Spinner size="sm" />
                  {zh.settings.embedding.reindexing}
                </>
              ) : (
                zh.settings.embedding.reindex
              )}
            </Button>
            <Button variant="secondary" onClick={() => void runTest()} disabled={test.isPending}>
              {test.isPending ? (
                <>
                  <Spinner size="sm" />
                  {zh.settings.common.testing}
                </>
              ) : (
                zh.settings.embedding.test
              )}
            </Button>
            <Button variant="primary" onClick={() => void save()} disabled={update.isPending}>
              {update.isPending ? zh.settings.common.saving : zh.settings.common.save}
            </Button>
          </>
        }
      >
        <ToggleRow
          title={zh.settings.embedding.enable}
          description={zh.settings.embedding.enableDesc}
          checked={form.enabled}
          onChange={(enabled) => patch({ enabled })}
        />

        <Field label={zh.settings.embedding.provider}>
          {(id) => (
            <Select
              id={id}
              value={form.provider}
              onChange={(e) => patch({ provider: e.target.value as EmbeddingProvider })}
            >
              {PROVIDERS.map((p) => (
                <option key={p} value={p}>
                  {zh.settings.embedding.providers[p]}
                </option>
              ))}
            </Select>
          )}
        </Field>

        <Field label={zh.settings.embedding.baseUrl}>
          {(id) => (
            <Input
              id={id}
              value={form.base_url}
              onChange={(e) => patch({ base_url: e.target.value })}
              placeholder={zh.settings.embedding.baseUrlPlaceholder}
              spellCheck={false}
            />
          )}
        </Field>

        <Field label={zh.settings.embedding.apiKey} hint={zh.settings.common.keyKeepHint}>
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
          <Field label={zh.settings.embedding.model}>
            {(id) => (
              <Input
                id={id}
                value={form.model}
                onChange={(e) => patch({ model: e.target.value })}
                placeholder={zh.settings.embedding.modelPlaceholder}
                spellCheck={false}
              />
            )}
          </Field>
          <Field label={zh.settings.embedding.dim} hint={zh.settings.embedding.dimHint}>
            {(id) => (
              <Input
                id={id}
                type="number"
                min={MIN_DIM}
                max={MAX_DIM}
                step={64}
                value={form.dim}
                onChange={(e) => {
                  const n = Number(e.target.value)
                  patch({ dim: Number.isFinite(n) ? n : form.dim })
                }}
              />
            )}
          </Field>
        </div>

        {dimMismatch !== null && (
          <WarningBanner>{zh.settings.embedding.dimMismatch(dimMismatch, form.dim)}</WarningBanner>
        )}
      </SettingsCard>

      <Modal
        open={confirmOpen}
        onClose={() => setConfirmOpen(false)}
        title={zh.settings.embedding.reindexTitle}
      >
        <p className="text-sm text-gray-600 dark:text-gray-300 leading-relaxed">
          {zh.settings.embedding.reindexConfirmDesc}
        </p>
        <div className="mt-6 flex justify-end gap-2">
          <Button variant="ghost" onClick={() => setConfirmOpen(false)}>
            {zh.common.cancel}
          </Button>
          <Button variant="danger" onClick={() => void runReindex()}>
            {zh.settings.embedding.reindexConfirm}
          </Button>
        </div>
      </Modal>
    </div>
  )
}
