/**
 * 流式类型（frontend-design.md §4 / §5）：
 * - StreamItem / Stage / StageType：会话流条目与 HITL StageCard 数据模型；
 * - SSE 事件类型：后端 canonical 事件表（backend-architecture.md §3.4）。
 */

import type {
  Artifact,
  ArtifactKind,
  FileMeta,
  IterationType,
  StepRunStatus,
} from './models'

// ---------------------------------------------------------------------------
// StreamItem / Stage（§4）
// ---------------------------------------------------------------------------

/** HITL 卡片类型（interaction_required.kind 与之同域）。 */
export type StageType =
  | 'intake'
  | 'type_suggest'
  | 'material_upload'
  | 'patent_points'
  | 'prior_art'
  | 'preview_confirm'
  | 'self_check'
  | 'claim_bias'
  | 'mode_select'
  | 'form'
  | 'review'
  | 'confirm'
  | 'oa_issues'
  | 'oa_strategy'
  | 'figures_preview'
  | 'delivery'

export type StageStatus = 'active' | 'completed' | 'skipped'

/**
 * HITL 阶段卡数据。未知 type 渲染通用 JSON 卡（§4），
 * 故允许后端扩展的任意字符串（保留 StageType 字面量自动补全）。
 */
export interface Stage {
  id: string
  type: StageType | (string & {})
  status: StageStatus
  /** interaction_required 原始载荷（schema / prompt / step_key 等）。 */
  payload: unknown
  /** 用户提交的结果（乐观 completed 时立即写入）。 */
  result?: unknown
  /** 关联流水线步骤。 */
  stepKey?: string
}

export interface UserStreamItem {
  kind: 'user'
  id: string
  text: string
  files?: FileMeta[]
}

export interface AssistantStreamItem {
  kind: 'assistant'
  id: string
  markdown: string
  streaming: boolean
  stepKey?: string | null
}

export interface StageStreamItem {
  kind: 'stage'
  id: string
  stage: Stage
}

export interface DocRefStreamItem {
  kind: 'doc_ref'
  id: string
  docId: string
}

export interface NoticeStreamItem {
  kind: 'notice'
  id: string
  text: string
  /** 同 tag 的连续 notice 允许原地更新（如 search_progress 滚动进度）。 */
  tag?: string
  /** error → 渲染为流内失败卡（默认 info：居中灰字）。 */
  level?: 'info' | 'error'
  /** error 且 retryable → 失败卡显示「重试此步」。 */
  retryable?: boolean
  /** 失败所属步骤（重试时作为 POST /pipeline/retry 的 step_key）。 */
  stepKey?: string
  /** 失败步骤中文名（失败卡标题用）。 */
  stepLabel?: string
}

export type StreamItem =
  | UserStreamItem
  | AssistantStreamItem
  | StageStreamItem
  | DocRefStreamItem
  | NoticeStreamItem

// ---------------------------------------------------------------------------
// SSE 事件（后端 canonical 事件表）
// ---------------------------------------------------------------------------

/** llm_delta 双通道：chat=会话叙述；doc=文档正文。 */
export type LlmChannel = 'chat' | 'doc'

export interface StepStatusEvent {
  step_key: string
  status: StepRunStatus
  name_zh?: string
  attempt?: number
  error?: string | null
}

export interface LlmDeltaEvent {
  step_key: string
  channel: LlmChannel
  text: string
  /** doc 通道目标文档 id；缺省归入主文档。 */
  doc_id?: string
}

export interface LlmDoneEvent {
  step_key: string
  channel: LlmChannel
  doc_id?: string
}

/** doc_version.downloads 条目（各格式交付物下载指引）。 */
export interface DocDownloadEntry {
  kind: ArtifactKind | (string & {})
  artifact_id: string
  filename: string
}

/** 文档定稿一个新版本（VersionHistory / DownloadMenu 数据源）。 */
export interface DocVersionEvent {
  doc_id?: string
  version_id: string
  version: number
  kind: ArtifactKind
  filename: string
  /** 主交付物 artifact id（下载 /artifacts/{id}/download）。 */
  artifact_id?: string
  /** 各格式下载项（契约：downloads:[{kind,artifact_id,filename}]）。 */
  downloads?: DocDownloadEntry[]
  iteration_type?: IterationType | null
  summary?: string | null
  created_at: string
}

export interface InteractionRequiredEvent {
  step_key: string
  kind: StageType | (string & {})
  schema?: unknown
  prompt?: string
  /** 表单默认值（契约：pending_interaction.default）。 */
  default?: unknown
}

export interface SearchProgressEvent {
  message: string
  phase?: string
  count?: number
}

/** artifact_created 载荷即 Artifact 记录。 */
export type ArtifactCreatedEvent = Artifact

export interface CaseTitleEvent {
  title: string
}

export interface LogEvent {
  message: string
  level?: 'info' | 'warning' | 'error'
}

export interface ErrorEvent {
  message: string
  retryable: boolean
  step_key?: string
}

export interface PipelineDoneEvent {
  run_group?: string
  status?: 'done' | 'failed' | 'cancelled'
}

export interface PingEvent {
  t: string
}

/** 事件名 → 载荷类型映射（canonical 事件表）。 */
export interface CaseSseEventMap {
  step_status: StepStatusEvent
  llm_delta: LlmDeltaEvent
  llm_done: LlmDoneEvent
  doc_version: DocVersionEvent
  interaction_required: InteractionRequiredEvent
  search_progress: SearchProgressEvent
  artifact_created: ArtifactCreatedEvent
  case_title: CaseTitleEvent
  log: LogEvent
  error: ErrorEvent
  pipeline_done: PipelineDoneEvent
  ping: PingEvent
}

export type CaseSseEventName = keyof CaseSseEventMap

/** 已解析的类型化事件（判别联合）。 */
export type CaseSseEvent = {
  [K in CaseSseEventName]: { event: K; data: CaseSseEventMap[K]; id?: string }
}[CaseSseEventName]

/** 事件名集合（运行时判别用）。 */
export const CASE_SSE_EVENT_NAMES: readonly CaseSseEventName[] = [
  'step_status',
  'llm_delta',
  'llm_done',
  'doc_version',
  'interaction_required',
  'search_progress',
  'artifact_created',
  'case_title',
  'log',
  'error',
  'pipeline_done',
  'ping',
] as const

export function isCaseSseEventName(name: string): name is CaseSseEventName {
  return (CASE_SSE_EVENT_NAMES as readonly string[]).includes(name)
}
