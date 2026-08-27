/**
 * 刷新恢复：把已落盘的「正文型」交付物挑出来，并把它渲染成文档面板能吃的 Markdown。
 *
 * 背景：sessionStore 的 docs[docId].markdown 只由实时 SSE 的
 * llm_delta(channel='doc') 累积。案件跑完后重新打开，事件流里没有 delta 可replay
 * （delta 不入库），面板就只剩一个空的 .md-prose。WorkspaceShell 在水合阶段用这里
 * 挑出交付物 → GET /artifacts/{id}/content → 回填 docs[docId].markdown。
 */

import { zh } from '../i18n/zh'
import type { Artifact, ArtifactKind, Module } from '../types/models'

/** 各模块的「正文型」交付物 kind（右侧文档面板回填的首选源）。 */
export const DOC_ARTIFACT_KIND: Record<Module, ArtifactKind> = {
  disclosure: 'disclosure_md',
  paper2patent: 'patent_content_json',
  reader: 'reader_note_md',
  oa: 'oa_response_md',
}

/**
 * 模块无关的正文型 kind 兜底顺序：
 * 案件详情里查不到本模块的首选 kind 时（老数据 / 模块字段缺失）按这个次序捡一个。
 */
const DOC_KIND_FALLBACK: readonly string[] = [
  'disclosure_md',
  'patent_content_json',
  'reader_note_md',
  'oa_response_md',
  'revision_log_md',
]

/** /artifacts/{id}/content 只认文本类扩展名，二进制交付物会被后端回 415。 */
const TEXT_EXTS = new Set(['md', 'markdown', 'json', 'txt', 'svg', 'yaml', 'yml'])

function extOf(filename: string | undefined): string {
  if (!filename) return ''
  const dot = filename.lastIndexOf('.')
  return dot >= 0 ? filename.slice(dot + 1).toLowerCase() : ''
}

function isTextArtifact(filename: string | undefined): boolean {
  return TEXT_EXTS.has(extOf(filename))
}

/** 回填候选来源（DocState 的最小子集，避免 lib 反向依赖 store）。 */
export interface DocArtifactHints {
  filename?: string
  artifactId?: string
  downloads?: readonly { kind?: unknown; artifact_id: string; filename: string }[]
}

/** 命中的回填源（只需要 id，kind/filename 用于决定怎么渲染）。 */
export interface DocArtifactRef {
  id: string
  kind: string
  filename: string
}

/**
 * 挑一个可回填的正文交付物。
 *
 * 次序：本模块首选 kind（案件详情的 artifacts_latest 即各 kind 最新版）→
 * 已 replay 的 doc_version 下载清单里的文本项 → doc_version 主交付物 →
 * 模块无关兜底。全部落空返回 null（新案件的正常状态）。
 */
export function pickDocArtifact(
  module: Module | undefined | null,
  artifactsLatest: readonly Artifact[] | undefined | null,
  hints?: DocArtifactHints | null,
): DocArtifactRef | null {
  const latest = artifactsLatest ?? []

  const preferred = module ? DOC_ARTIFACT_KIND[module] : undefined
  if (preferred) {
    const hit = latest.find((a) => a.kind === preferred && isTextArtifact(a.filename))
    if (hit) return { id: hit.id, kind: hit.kind, filename: hit.filename }
  }

  for (const entry of hints?.downloads ?? []) {
    if (isTextArtifact(entry.filename)) {
      return { id: entry.artifact_id, kind: String(entry.kind ?? ''), filename: entry.filename }
    }
  }

  if (hints?.artifactId && isTextArtifact(hints.filename)) {
    return { id: hints.artifactId, kind: '', filename: hints.filename ?? '' }
  }

  for (const kind of DOC_KIND_FALLBACK) {
    const hit = latest.find((a) => a.kind === kind && isTextArtifact(a.filename))
    if (hit) return { id: hit.id, kind: hit.kind, filename: hit.filename }
  }
  return null
}

// ---------------------------------------------------------------------------
// 正文渲染
// ---------------------------------------------------------------------------

function section(title: string, body: string | undefined, level = '##'): string[] {
  const text = (body ?? '').trim()
  return text ? [`${level} ${title}`, '', text, ''] : []
}

function asStrings(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.map((v) => (typeof v === 'string' ? v.trim() : '')).filter(Boolean)
}

/**
 * PatentContent（论文转专利的 patent_content_json）→ 可读 Markdown。
 * 直接把 JSON 原文丢进 md-prose 会渲染成一大坨无格式文本，比空白还难看。
 */
function patentContentToMarkdown(content: Record<string, unknown>): string {
  const t = zh.document.patentSections
  const name = typeof content.invention_name === 'string' ? content.invention_name.trim() : ''
  const desc = (content.description ?? {}) as Record<string, unknown>
  const claims = asStrings(content.claims)
  const drawingDesc = asStrings(desc.drawing_description)

  const lines: string[] = []
  if (name) lines.push(`# ${name}`, '')
  lines.push(...section(t.abstract, content.abstract as string | undefined))
  if (claims.length > 0) {
    lines.push(`## ${t.claims}`, '', ...claims.flatMap((c) => [c, '']))
  }
  const body = [
    ...section(t.technicalField, desc.technical_field as string | undefined, '###'),
    ...section(t.background, desc.background as string | undefined, '###'),
    ...section(t.inventionContent, desc.invention_content as string | undefined, '###'),
    ...(drawingDesc.length > 0
      ? [`### ${t.drawingDescription}`, '', ...drawingDesc.map((d) => `- ${d}`), '']
      : []),
    ...section(t.embodiments, desc.embodiments as string | undefined, '###'),
  ]
  if (body.length > 0) lines.push(`## ${t.description}`, '', ...body)
  return lines.join('\n').trim()
}

/**
 * 交付物原文 → 文档面板 Markdown。
 * md/txt 原样透传；patent_content_json 按契约装配成小节；其余 JSON 包一层代码块。
 */
export function artifactToMarkdown(filename: string, content: string): string {
  if (extOf(filename) !== 'json') return content
  let parsed: unknown
  try {
    parsed = JSON.parse(content)
  } catch {
    return content
  }
  if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
    const obj = parsed as Record<string, unknown>
    if (typeof obj.invention_name === 'string' || Array.isArray(obj.claims)) {
      const md = patentContentToMarkdown(obj)
      if (md) return md
    }
  }
  return ['```json', content.trim(), '```'].join('\n')
}
