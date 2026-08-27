/**
 * API 模型（对照 backend/app/models/*.py 与 api/*.py 的实际返回结构）。
 * 后端为 pydantic snake_case，前端类型 1:1 镜像字段名，不做驼峰转换。
 */

/** 四大模块（backend models/case.py: Module）。 */
export type Module = 'disclosure' | 'paper2patent' | 'reader' | 'oa'

/** 专利类型。 */
export type PatentType = 'invention' | 'utility_model' | 'design'

/** 案件状态。 */
export type CaseStatus =
  | 'draft'
  | 'running'
  | 'waiting_user'
  | 'completed'
  | 'failed'
  | 'archived'

/** 文头联系人（可占位）。 */
export interface Contact {
  name: string
  phone: string
  email: string
}

/** POST /cases 请求体。 */
export interface CaseCreate {
  module: Module
  title?: string | null
  patent_type?: PatentType | null
}

/** PATCH /cases/{id} 请求体。 */
export interface CaseUpdate {
  title?: string | null
  status?: 'draft' | 'archived' | null
  patent_type?: PatentType | null
  contact?: Contact | null
}

/** 案件列表项（GET /cases items / POST /cases 返回）。 */
export interface Case {
  id: string
  module: Module
  title: string
  patent_type: PatentType | null
  status: CaseStatus
  current_step: string | null
  contact: Contact | null
  created_at: string
  updated_at: string
}

/**
 * GET /cases/{id}：案件 + 流水线工作内存 + 各 kind 最新版交付物。
 *
 * 归属字段（M8）：普通用户永远只能取到自己的案件（他人的一律 404），
 * 只有管理员会看到 `user_id` 与自己不同的案件 —— 此时工作台顶部必须挂
 * 跨用户提示条（auth-system.md §4.2）。这些字段标为可选：后端若尚未
 * 在 CaseOut 上补归属信息，前端只是不显示提示条，不会报错。
 */
export interface CaseDetail extends Case {
  state: Record<string, unknown>
  artifacts_latest: Artifact[]
  /** 案件属主 user_id（后端可能命名为 user_id 或 owner_id，两者都认）。 */
  user_id?: string | null
  owner_id?: string | null
  /** 属主用户名 / 显示名（后端若一并下发则直接用，省一次查表）。 */
  owner_username?: string | null
  owner_display_name?: string | null
}

/** 会话消息（GET /cases/{id}/messages；SSE 重放源）。 */
export interface Message {
  id: string
  case_id: string
  seq: number
  role: 'user' | 'assistant' | 'system' | 'event'
  step_key: string | null
  content: string
  meta: Record<string, unknown> | null
  created_at: string
}

/** 文件种类（backend models/file.py: FileKind）。 */
export type FileKind = 'upload' | 'converted_md' | 'extracted_figure' | 'manual'

/** 文件记录（GET /cases/{id}/files 等）。 */
export interface FileOut {
  id: string
  case_id: string
  kind: FileKind
  orig_name: string
  mime: string | null
  size: number | null
  stored_path: string
  md_path: string | null
  /** {digest, pages, figure_captions[], convert_error} */
  meta: Record<string, unknown> | null
  created_at: string
}

/**
 * 前端轻量文件元信息（composer 附件 pill / user 消息附件等尚未落库的场景）。
 * 已落库文件请用 FileOut。
 */
export interface FileMeta {
  id?: string
  name: string
  size?: number
  mime?: string
}

/** 交付物种类（backend models/artifact.py: ArtifactKind）。 */
export type ArtifactKind =
  | 'disclosure_md'
  | 'disclosure_docx'
  | 'disclosure_pdf'
  | 'patent_content_json'
  | 'patent_docx'
  | 'patent_pdf'
  | 'figure_svg'
  | 'figure_png'
  | 'mermaid_png'
  | 'reader_note_md'
  | 'reader_note_docx'
  | 'oa_response_md'
  | 'oa_response_docx'
  | 'revision_log_md'
  | 'search_report_json'

/** 迭代类型（版本历史标签：初稿/合并迭代/纠正迭代/重建/导出）。 */
export type IterationType = 'initial' | 'merge' | 'correction' | 'rebuild' | 'export'

/** 交付物记录（版本化只增不改）。 */
export interface Artifact {
  id: string
  case_id: string
  version: number
  kind: ArtifactKind
  filename: string
  stored_path: string
  run_group: string | null
  iteration_type: IterationType | null
  summary: string | null
  source_artifact_id: string | null
  created_at: string
}

/** 流水线单步执行状态（pipeline_runs.status）。 */
export type StepRunStatus =
  | 'pending'
  | 'running'
  | 'waiting_user'
  | 'done'
  | 'failed'
  | 'skipped'
  | 'cancelled'

/** GET /cases/{id}/pipeline/state 中的单步。 */
export interface PipelineStep {
  key: string
  name_zh: string
  status: StepRunStatus
  attempt?: number
  error?: string | null
}

/** GET /cases/{id}/pipeline/state。 */
export interface PipelineState {
  run_group: string | null
  steps: PipelineStep[]
  pending_interaction: {
    step_key: string
    kind: string
    schema?: unknown
    prompt?: string
  } | null
}

/** POST /cases/{id}/pipeline/input 请求体。 */
export interface PipelineInput {
  step_key: string
  payload: unknown
}

/** 通用分页（backend models/common.py: Page）。 */
export interface Page<T> {
  items: T[]
  total: number
  limit: number
  offset: number
}

/** 通用成功响应。 */
export interface Ok {
  ok: boolean
}
