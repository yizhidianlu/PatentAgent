import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../lib/api'
import type {
  Artifact,
  ArtifactKind,
  CaseDetail,
  Message,
  PipelineInput,
  PipelineState,
} from '../types/models'

/**
 * 流水线 react-query hooks（流水线 API 契约，backend-architecture.md §API）：
 * - POST /cases/{id}/pipeline/start  → 202 {run_group}（409 = 已在运行）
 * - GET  /cases/{id}/pipeline/state  → {run_group, steps[], pending_interaction}
 * - POST /cases/{id}/pipeline/input  {step_key, payload} → 202（非 waiting_user 时 409）
 * - POST /cases/{id}/pipeline/resume | cancel → 202；/pipeline/retry {step_key?} → 202
 * 以及 GET /cases/{id}（详情）、GET /cases/{id}/messages（快照）、GET /cases/{id}/artifacts。
 */

// ---------------------------------------------------------------------------
// query keys
// ---------------------------------------------------------------------------

export const pipelineKeys = {
  detail: (caseId: string) => ['case', caseId] as const,
  messages: (caseId: string) => ['case', caseId, 'messages'] as const,
  state: (caseId: string) => ['case', caseId, 'pipeline-state'] as const,
  artifacts: (caseId: string) => ['case', caseId, 'artifacts'] as const,
  artifactContent: (artifactId: string) => ['artifact', artifactId, 'content'] as const,
}

// ---------------------------------------------------------------------------
// 查询
// ---------------------------------------------------------------------------

/** 单案详情（案件 + 流水线工作内存 + 各类交付物最新版）。 */
export function useCaseDetail(caseId: string | undefined, enabled = true) {
  return useQuery({
    queryKey: pipelineKeys.detail(caseId ?? ''),
    queryFn: () => api.get<CaseDetail>(`/cases/${encodeURIComponent(caseId!)}`),
    enabled: Boolean(caseId) && enabled,
    retry: 1,
  })
}

/** 会话历史快照（SSE 重放兜底；after_seq 之后）。 */
export function useCaseMessages(caseId: string | undefined, enabled = true) {
  return useQuery({
    queryKey: pipelineKeys.messages(caseId ?? ''),
    queryFn: () => api.get<Message[]>(`/cases/${encodeURIComponent(caseId!)}/messages`),
    enabled: Boolean(caseId) && enabled,
    retry: 1,
    staleTime: Infinity,
  })
}

/** 流水线状态（刷新恢复 / 轮询兜底）。 */
export function usePipelineState(caseId: string | undefined, enabled = true) {
  return useQuery({
    queryKey: pipelineKeys.state(caseId ?? ''),
    queryFn: () =>
      api.get<PipelineState>(`/cases/${encodeURIComponent(caseId!)}/pipeline/state`),
    enabled: Boolean(caseId) && enabled,
    retry: 1,
  })
}

/** 案件交付物全部版本（kind 可过滤）。 */
export function useArtifacts(caseId: string | undefined, kind?: string, enabled = true) {
  return useQuery({
    queryKey: [...pipelineKeys.artifacts(caseId ?? ''), kind ?? 'all'] as const,
    queryFn: () =>
      api.get<Artifact[]>(`/cases/${encodeURIComponent(caseId!)}/artifacts`, {
        query: { kind },
      }),
    enabled: Boolean(caseId) && enabled,
    retry: 1,
  })
}

/** GET /artifacts/{id}/content 返回体（backend models/artifact.py: ArtifactContentOut）。 */
export interface ArtifactContent {
  id: string
  kind: ArtifactKind
  version: number
  filename: string
  content: string
}

/**
 * 文本类交付物全文（md / json / txt / svg；二进制交付物后端回 415）。
 *
 * 这是**刷新后右侧文档面板正文的唯一来源**：sessionStore 的 docs[].markdown
 * 只由实时 SSE 的 llm_delta(channel='doc') 累积，重开一个已交付的案件时它是空的，
 * 必须靠这里把已落盘的交付物正文回填回去（详见 WorkspaceShell 的文档水合）。
 */
export function useArtifactContent(artifactId: string | null | undefined, enabled = true) {
  return useQuery({
    queryKey: pipelineKeys.artifactContent(artifactId ?? ''),
    queryFn: () =>
      api.get<ArtifactContent>(`/artifacts/${encodeURIComponent(artifactId!)}/content`),
    enabled: Boolean(artifactId) && enabled,
    retry: 1,
    staleTime: Infinity,
  })
}

// ---------------------------------------------------------------------------
// 命令（POST 返回 202；一切输出经 SSE 通道）
// ---------------------------------------------------------------------------

export interface PipelineStartResult {
  run_group: string
}

/** 启动流水线（body = 模块初始载荷；409 = 已在运行）。 */
export function useStartPipeline(caseId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: unknown) =>
      api.post<PipelineStartResult>(
        `/cases/${encodeURIComponent(caseId)}/pipeline/start`,
        payload,
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: pipelineKeys.state(caseId) })
    },
  })
}

/** 提交 HITL 交互输入（非 waiting_user 时 409）。 */
export function useSubmitInput(caseId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (input: PipelineInput) =>
      api.post<unknown>(`/cases/${encodeURIComponent(caseId)}/pipeline/input`, input),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: pipelineKeys.state(caseId) })
    },
  })
}

/** 恢复暂停的流水线。 */
export function useResumePipeline(caseId: string) {
  return useMutation({
    mutationFn: () =>
      api.post<unknown>(`/cases/${encodeURIComponent(caseId)}/pipeline/resume`),
  })
}

/** 重试（可指定 step_key，缺省重试失败步骤）。 */
export function useRetryPipeline(caseId: string) {
  return useMutation({
    mutationFn: (stepKey?: string) =>
      api.post<unknown>(
        `/cases/${encodeURIComponent(caseId)}/pipeline/retry`,
        stepKey ? { step_key: stepKey } : {},
      ),
  })
}

/** 取消当前轮次。 */
export function useCancelPipeline(caseId: string) {
  return useMutation({
    mutationFn: () =>
      api.post<unknown>(`/cases/${encodeURIComponent(caseId)}/pipeline/cancel`),
  })
}

/** 简称别名（COMMON 契约命名）。 */
export const useResume = useResumePipeline
export const useRetry = useRetryPipeline
export const useCancel = useCancelPipeline

// ---------------------------------------------------------------------------
// p2p 专利内容（content_review 卡）
// ---------------------------------------------------------------------------

/** PatentContent JSON（backend models/paper2patent.py；宽松镜像，容忍字段演进）。 */
export interface PatentContent {
  invention_name?: string
  abstract?: string
  abstract_drawing?: unknown
  claims?: unknown[]
  description?: Record<string, unknown>
  drawings?: unknown[]
  [key: string]: unknown
}

export function useP2pContent(caseId: string | undefined, enabled = true) {
  return useQuery({
    queryKey: ['case', caseId ?? '', 'p2p-content'] as const,
    queryFn: () =>
      api.get<PatentContent>(`/cases/${encodeURIComponent(caseId!)}/p2p/content`),
    enabled: Boolean(caseId) && enabled,
    retry: 1,
  })
}

/** PUT /cases/{id}/p2p/content（触发校验 + lint，产新版本）。 */
export function useUpdateP2pContent(caseId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (content: PatentContent) =>
      api.put<PatentContent>(`/cases/${encodeURIComponent(caseId)}/p2p/content`, content),
    onSuccess: (data) => {
      queryClient.setQueryData(['case', caseId, 'p2p-content'], data)
    },
  })
}
