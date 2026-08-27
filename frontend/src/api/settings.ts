import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../lib/api'
import { zh } from '../i18n/zh'
import { useUiStore } from '../stores/uiStore'

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

export interface LlmTestResult {
  ok: boolean
  model: string | null
  latency_ms: number | null
  error: string | null
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
