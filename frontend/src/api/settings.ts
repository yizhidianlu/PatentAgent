import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../lib/api'
import { zh } from '../i18n/zh'
import { useUiStore } from '../stores/uiStore'
import type { ModelTier } from './sessions'

/**
 * 设置页 react-query hooks（backend-architecture.md §3.1 settings）：
 *
 * | 方法 | 路径 |
 * |---|---|
 * | GET/PUT | `/settings/llm`、`/settings/embedding`、`/settings/image-gen` |
 * | POST | `/settings/{llm,embedding,image-gen}/test`（请求体可临时覆盖未保存的表单值） |
 * | POST | `/settings/embedding/reindex` |
 * | GET | `/system/env` |
 *
 * api_key 约定：GET 只回掩码（`sk-***尾4位`）；PUT 提交空串 / 掩码 = 不修改已存 key。
 * 因此表单可以把 GET 到的掩码原样回传，用户留空亦不会清空后端的真 key。
 */

// ---------------------------------------------------------------------------
// 类型（与 backend/app/models/settings.py 一一对应）
// ---------------------------------------------------------------------------

export interface LlmSettings {
  base_url: string
  api_key: string
  model: string
  temperature: number
  max_output_tokens: number
  context_window: number
  supports_json_mode: boolean
}

/** 一档模型：留空的字段沿用主配置（`settings.llm`）。 */
export type ThinkingMode = 'auto' | 'enabled' | 'disabled'
export type ReasoningEffort = 'auto' | 'low' | 'medium' | 'high'

export interface LlmTierSettings {
  model: string
  label: string
  /** 思考模式：auto=沿用主配置 / 服务商默认。「快档」真正的开关。 */
  thinking: ThinkingMode
  reasoning_effort: ReasoningEffort
  /** 空 = 沿用主配置的服务地址。 */
  base_url: string
  /** 空 = 沿用主配置的密钥；换 host 时后端会要求必须填。 */
  api_key: string
  temperature: number | null
  max_output_tokens: number | null
  context_window: number | null
  supports_json_mode: boolean | null
}

export interface ModelTiersSettings {
  fast: LlmTierSettings
  deep: LlmTierSettings
  default_tier: ModelTier
}

/** GET/PUT /settings/model-tiers 的响应：比请求体多「实际生效的模型名」。 */
export interface ModelTiersOut extends ModelTiersSettings {
  base_model: string
  base_url: string
  effective: Record<string, string>
  /** 各档实际会打到的服务地址（留空的档位显示回落到的那个）。 */
  effective_base_url: Record<string, string>
}

export function emptyTier(): LlmTierSettings {
  return {
    model: '',
    label: '',
    thinking: 'auto',
    reasoning_effort: 'auto',
    base_url: '',
    api_key: '',
    temperature: null,
    max_output_tokens: null,
    context_window: null,
    supports_json_mode: null,
  }
}

/** 从服务商 /v1/models 探测到的模型规格（探测不到的字段为 null）。 */
export interface ModelCapability {
  context_length: number | null
  max_output_tokens: number | null
  supports_reasoning: boolean | null
  /** true 表示该值是服务端按上下文推荐的，而非厂商明示 */
  estimated: boolean
}

export interface LlmTestResult {
  ok: boolean
  model: string | null
  latency_ms: number | null
  error: string | null
  capability?: ModelCapability | null
  /** 这次实际打到的服务地址：model 只是服务端回声，认不出「打的是不是那家」。 */
  target_base_url?: string | null
}

/** 连接测试请求体：全部可选，缺省字段回落已存配置。 */
export interface LlmTestPayload {
  base_url?: string
  api_key?: string
  model?: string
  temperature?: number
}

export type EmbeddingProvider = 'zhipu' | 'dashscope' | 'minimax' | 'local' | 'custom'

export interface EmbeddingSettings {
  enabled: boolean
  provider: EmbeddingProvider
  base_url: string
  api_key: string
  model: string
  dim: number
}

export interface EmbeddingUpdateResult {
  settings: EmbeddingSettings
  need_rebuild: boolean
}

export interface EmbeddingTestResult {
  ok: boolean
  model: string | null
  dim: number | null
  latency_ms: number | null
  error: string | null
}

export interface EmbeddingTestPayload {
  base_url?: string
  api_key?: string
  model?: string
  /** 表单里的维度：测试要校验的是屏幕上这份配置，不是库里存着的旧值 */
  dim?: number
}

export interface EmbeddingReindexResult {
  ok: boolean
  queued: number
  dim: number
  vec_available: boolean
  message: string
}

export type ImageGenProvider = 'zhipu' | 'dashscope' | 'openai' | 'local' | 'custom'

export interface ImageGenSettings {
  enabled: boolean
  provider: ImageGenProvider
  base_url: string
  api_key: string
  model: string
  size: string
}

export interface ImageGenTestResult {
  ok: boolean
  model: string | null
  latency_ms: number | null
  image_url: string | null
  image_data_url: string | null
  error: string | null
}

export interface ImageGenTestPayload {
  base_url?: string
  api_key?: string
  model?: string
  size?: string
  prompt?: string
}

/** GET /system/env 探测结果（后端 api/system.py env()）。 */
export interface SystemEnv {
  python: { version: string; executable: string; platform: string }
  word: { available: boolean; path: string | null }
  soffice: { available: boolean; path: string | null }
  chrome: { available: boolean; path: string | null }
  edge: { available: boolean; path: string | null }
  sqlite_vec: { loaded: boolean; error: string | null; version: string | null }
  playwright: { importable: boolean }
  data_dir: {
    path: string
    disk: { total_gb?: number; free_gb?: number; error?: string }
  }
  frontend_dist: { available: boolean }
}

// ---------------------------------------------------------------------------
// 错误规整 + toast
// ---------------------------------------------------------------------------

/** 把任意异常规整为可展示的中文消息。 */
export function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError) return error.message || fallback
  if (error instanceof Error && error.message) return error.message
  return fallback
}

function toastError(error: unknown, fallback: string): void {
  useUiStore.getState().pushToast('error', errorMessage(error, fallback))
}

function toastSuccess(message: string): void {
  useUiStore.getState().pushToast('success', message)
}

export const settingsKeys = {
  all: ['settings'] as const,
  llm: ['settings', 'llm'] as const,
  embedding: ['settings', 'embedding'] as const,
  imageGen: ['settings', 'image-gen'] as const,
  modelTiers: ['settings', 'model-tiers'] as const,
  systemEnv: ['system', 'env'] as const,
}

/** 设置项极少变动：不自动重取，保存后手动 invalidate。 */
const SETTINGS_QUERY_OPTIONS = {
  staleTime: 5 * 60 * 1000,
  refetchOnWindowFocus: false,
  retry: 1,
} as const

// ---------------------------------------------------------------------------
// LLM
// ---------------------------------------------------------------------------

export function useLlmSettings() {
  return useQuery({
    queryKey: settingsKeys.llm,
    queryFn: () => api.get<LlmSettings>('/settings/llm'),
    ...SETTINGS_QUERY_OPTIONS,
  })
}

export function useUpdateLlm() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: LlmSettings) => api.put<LlmSettings>('/settings/llm', payload),
    onSuccess: (data) => {
      queryClient.setQueryData(settingsKeys.llm, data)
      toastSuccess(zh.settings.common.saved)
    },
    onError: (error) => toastError(error, zh.settings.common.saveFailed),
  })
}

/** 连接测试：传入表单当前（可能未保存）的值做临时覆盖。 */
export function useTestLlm() {
  return useMutation({
    mutationFn: (payload: LlmTestPayload) => api.post<LlmTestResult>('/settings/llm/test', payload),
    onError: (error) => toastError(error, zh.settings.model.testFailed),
  })
}


// ---------------------------------------------------------------------------
// 模型档位（快速 / 深度思考）
// ---------------------------------------------------------------------------

/**
 * 读两档配置。**普通用户也能读**——聊天框上的档位开关要知道有哪些档、各是什么模型。
 * 只有写需要管理员。
 */
export function useModelTiers() {
  return useQuery({
    queryKey: settingsKeys.modelTiers,
    queryFn: () => api.get<ModelTiersOut>('/settings/model-tiers'),
    ...SETTINGS_QUERY_OPTIONS,
  })
}

export function useUpdateModelTiers() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: ModelTiersSettings) =>
      api.put<ModelTiersOut>('/settings/model-tiers', payload),
    onSuccess: (data) => {
      queryClient.setQueryData(settingsKeys.modelTiers, data)
      toastSuccess(zh.settings.common.saved)
    },
    onError: (error) => toastError(error, zh.settings.common.saveFailed),
  })
}


/** 按**已保存**的档位配置试连。配错了要在跑 40 分钟之前就知道。 */
export function useTestModelTier() {
  return useMutation({
    mutationFn: (tier: ModelTier) =>
      api.post<LlmTestResult>(
        `/settings/model-tiers/${encodeURIComponent(tier)}/test`,
        {},
      ),
  })
}

// ---------------------------------------------------------------------------
// Embedding
// ---------------------------------------------------------------------------

export function useEmbeddingSettings() {
  return useQuery({
    queryKey: settingsKeys.embedding,
    queryFn: () => api.get<EmbeddingSettings>('/settings/embedding'),
    ...SETTINGS_QUERY_OPTIONS,
  })
}

export function useUpdateEmbedding() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: EmbeddingSettings) =>
      api.put<EmbeddingUpdateResult>('/settings/embedding', payload),
    onSuccess: (data) => {
      queryClient.setQueryData(settingsKeys.embedding, data.settings)
      toastSuccess(zh.settings.common.saved)
    },
    onError: (error) => toastError(error, zh.settings.common.saveFailed),
  })
}

export function useTestEmbedding() {
  return useMutation({
    mutationFn: (payload: EmbeddingTestPayload) =>
      api.post<EmbeddingTestResult>('/settings/embedding/test', payload),
    onError: (error) => toastError(error, zh.settings.embedding.testFailed),
  })
}

export function useReindexEmbedding() {
  return useMutation({
    mutationFn: () => api.post<EmbeddingReindexResult>('/settings/embedding/reindex'),
    onSuccess: (data) => toastSuccess(data.message || zh.settings.embedding.reindexDone(data.queued)),
    onError: (error) => toastError(error, zh.settings.embedding.reindexFailed),
  })
}

// ---------------------------------------------------------------------------
// 图像生成
// ---------------------------------------------------------------------------

export function useImageGenSettings() {
  return useQuery({
    queryKey: settingsKeys.imageGen,
    queryFn: () => api.get<ImageGenSettings>('/settings/image-gen'),
    ...SETTINGS_QUERY_OPTIONS,
  })
}

export function useUpdateImageGen() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: ImageGenSettings) =>
      api.put<ImageGenSettings>('/settings/image-gen', payload),
    onSuccess: (data) => {
      queryClient.setQueryData(settingsKeys.imageGen, data)
      toastSuccess(zh.settings.common.saved)
    },
    onError: (error) => toastError(error, zh.settings.common.saveFailed),
  })
}

export function useTestImageGen() {
  return useMutation({
    mutationFn: (payload: ImageGenTestPayload) =>
      api.post<ImageGenTestResult>('/settings/image-gen/test', payload),
    onError: (error) => toastError(error, zh.settings.imageGen.testFailed),
  })
}

// ---------------------------------------------------------------------------
// 系统环境
// ---------------------------------------------------------------------------

export function useSystemEnv(enabled = true) {
  return useQuery({
    queryKey: settingsKeys.systemEnv,
    queryFn: () => api.get<SystemEnv>('/system/env'),
    enabled,
    staleTime: 60 * 1000,
    refetchOnWindowFocus: false,
    retry: 1,
  })
}
