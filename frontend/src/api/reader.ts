/**
 * 专利解读（模块 C）数据契约与 react-query hooks。
 *
 * 后端产物（prompt-porting-spec.md 模块 C / backend-architecture.md §4 模块③）：
 * - C2 `claim_tree.json`  → {claims:[{no,type,refs,ref_mode,preamble,features[]}], ambiguities[]}
 * - C3 `claim_deltas.json` → {deltas:[{claim,delta}]}
 * - C6 `report.json`      → {meta, sections:[{blocks:[{type: markdown|table|claim_tree|mermaid|callout-*}]}]}
 *
 * 后端字段命名可能演进，故一律经此处的宽松规整函数读取：
 * 缺字段降级为空值而非崩溃（与 components/stages/stagePayload.ts 同一约定）。
 */

import { useQuery } from '@tanstack/react-query'
import { api, ApiError } from '../lib/api'

// ---------------------------------------------------------------------------
// 权利要求树
// ---------------------------------------------------------------------------

export type ClaimType = 'independent' | 'dependent'

/** 引用模式（backend assets/prompts/reader/claim_tree.md）。 */
export type ClaimRefMode = 'none' | 'single' | 'alternative' | 'range' | 'multiple'

export interface ClaimFeature {
  fid: string
  text: string
}

export interface ClaimNode {
  no: number
  type: ClaimType
  /** 引用的权利要求号（首位 = 主引用）。 */
  refs: number[]
  refMode: ClaimRefMode
  preamble: string
  features: ClaimFeature[]
  /** 每权白话增量（C3 产物合并进来）。 */
  delta: string
}

/** C2 登记的疑难项（多引消歧 / 编号 / 引用基础 / OCR）。 */
export interface ClaimAmbiguity {
  claim: number
  kind: string
  issue: string
  candidates: number[]
  chosen: number | null
  reason: string
}

export interface ClaimTreeData {
  claims: ClaimNode[]
  ambiguities: ClaimAmbiguity[]
}

/** 递归渲染用的树节点。 */
export interface ClaimTreeNode {
  claim: ClaimNode
  /** 主引用（挂载父节点）；根节点为 null。 */
  parentNo: number | null
  /** 主引用之外的其余引用（多引 → 重复链接 chips）。 */
  extraRefs: number[]
  children: ClaimTreeNode[]
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

function toStr(value: unknown): string {
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return ''
}

function toNum(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string' && value.trim() !== '' && Number.isFinite(Number(value))) {
    return Number(value)
  }
  return null
}

function toNumList(value: unknown): number[] {
  return asArray(value)
    .map(toNum)
    .filter((n): n is number => n !== null)
}

const REF_MODES: readonly ClaimRefMode[] = ['none', 'single', 'alternative', 'range', 'multiple']

function toRefMode(value: unknown, refs: number[]): ClaimRefMode {
  const raw = toStr(value)
  if ((REF_MODES as readonly string[]).includes(raw)) return raw as ClaimRefMode
  if (refs.length === 0) return 'none'
  return refs.length === 1 ? 'single' : 'multiple'
}

/**
 * 从任意载荷取出 claim_tree，兼容四种形态：
 * `{claim_tree:{…}}` / `{tree:{…}}`（`GET /reader/claim-tree` 出参）/
 * `{claims:[…]}`（claim_tree 门控 default）/ `[ … ]`；
 * 以及报告 claim_tree block 的 `{summary, rows:[…], markdown}`（rows = 权项行）。
 */
function pickTreeRecord(raw: unknown): Record<string, unknown> | null {
  if (Array.isArray(raw)) return { claims: raw }
  const record = asRecord(raw)
  if (!record) return null
  for (const key of ['claim_tree', 'claimTree', 'tree']) {
    const nested = asRecord(record[key])
    if (nested) return nested
  }
  if (Array.isArray(record.claims)) return record
  if (Array.isArray(record.rows)) return { ...record, claims: record.rows }
  return null
}

/** deltas 载荷 → {权号: 白话增量}（兼容 {deltas:[…]} / [ … ] / {"1":"…"}）。 */
function pickDeltaMap(raw: unknown): Map<number, string> {
  const map = new Map<number, string>()
  const record = asRecord(raw)
  const list = Array.isArray(raw)
    ? raw
    : asArray(record?.deltas ?? record?.claim_deltas ?? record?.claimDeltas)

  for (const item of list) {
    const entry = asRecord(item)
    if (!entry) continue
    const no = toNum(entry.claim ?? entry.no ?? entry.claim_no)
    const text = toStr(entry.delta ?? entry.text ?? entry.summary)
    if (no !== null && text.trim()) map.set(no, text.trim())
  }

  // {"1": "…", "2": "…"} 形态
  if (map.size === 0 && record) {
    for (const [key, value] of Object.entries(record)) {
      const no = toNum(key)
      const text = toStr(value)
      if (no !== null && text.trim()) map.set(no, text.trim())
    }
  }
  return map
}

/**
 * 规整权要树：容忍 claims / claim_tree 嵌套、字符串权号、缺失 features；
 * `deltas` 可另行传入（C3 产物晚于 C2 到达）。
 */
export function normalizeClaimTree(raw: unknown, deltas?: unknown): ClaimTreeData | null {
  const record = pickTreeRecord(raw)
  if (!record) return null

  // `GET /reader/claim-tree` 把 deltas 放在 tree 之外的外层，故三处都找一遍
  const outer = asRecord(raw)
  const deltaMap = pickDeltaMap(
    deltas ?? record.deltas ?? record.claim_deltas ?? outer?.deltas ?? outer?.claim_deltas,
  )

  const claims: ClaimNode[] = []
  for (const item of asArray(record.claims)) {
    const entry = asRecord(item)
    if (!entry) continue
    const no = toNum(entry.no ?? entry.claim ?? entry.number)
    if (no === null) continue

    const refs = toNumList(entry.refs ?? entry.ref ?? entry.depends_on).filter((r) => r !== no)
    const typeRaw = toStr(entry.type ?? entry.claim_type)
    const type: ClaimType =
      typeRaw === 'independent' || typeRaw === 'dependent'
        ? typeRaw
        : refs.length > 0
          ? 'dependent'
          : 'independent'

    const features: ClaimFeature[] = asArray(entry.features)
      .map((f, i) => {
        const fr = asRecord(f)
        const text = fr ? toStr(fr.text ?? fr.feature ?? fr.content) : toStr(f)
        const fid = fr ? toStr(fr.fid ?? fr.id) : ''
        return { fid: fid || `${no}-F${i + 1}`, text }
      })
      .filter((f) => f.text.trim() !== '')

    claims.push({
      no,
      type,
      refs,
      refMode: toRefMode(entry.ref_mode ?? entry.refMode, refs),
      preamble: toStr(entry.preamble ?? entry.premise ?? entry.text),
      features,
      delta: deltaMap.get(no) ?? toStr(entry.delta),
    })
  }

  claims.sort((a, b) => a.no - b.no)

  const ambiguities: ClaimAmbiguity[] = asArray(record.ambiguities)
    .map((item) => {
      const entry = asRecord(item)
      if (!entry) return null
      const claim = toNum(entry.claim ?? entry.no)
      const issue = toStr(entry.issue ?? entry.message ?? entry.description)
      if (claim === null && !issue) return null
      return {
        claim: claim ?? 0,
        kind: toStr(entry.kind ?? entry.type) || 'other',
        issue,
        candidates: toNumList(entry.candidates ?? entry.refs),
        chosen: toNum(entry.chosen ?? entry.selected),
        reason: toStr(entry.reason ?? entry.rationale),
      }
    })
    .filter((a): a is ClaimAmbiguity => a !== null)

  if (claims.length === 0 && ambiguities.length === 0) return null
  return { claims, ambiguities }
}

/**
 * 扁平权项 → 递归树：以 `refs[0]` 为主引用挂载父节点；
 * 引用号 ≥ 自身、指向不存在的权项、或形成环时一律提升为根（树 lint 由后端把关，
 * 前端只保证不因脏数据递归爆栈）。
 */
export function buildClaimTree(claims: ClaimNode[]): ClaimTreeNode[] {
  const byNo = new Map<number, ClaimNode>()
  for (const claim of claims) byNo.set(claim.no, claim)

  const nodes = new Map<number, ClaimTreeNode>()
  for (const claim of claims) {
    const validRefs = claim.refs.filter((r) => r < claim.no && byNo.has(r))
    const parentNo = validRefs.length > 0 ? validRefs[0] : null
    nodes.set(claim.no, {
      claim,
      parentNo,
      extraRefs: validRefs.slice(1),
      children: [],
    })
  }

  const roots: ClaimTreeNode[] = []
  for (const node of nodes.values()) {
    const parent = node.parentNo !== null ? nodes.get(node.parentNo) : undefined
    if (parent && parent !== node) parent.children.push(node)
    else roots.push(node)
  }
  return roots
}

/**
 * 报告里的 claim_tree block 只带「结构 / 权 / 本项新增」三列（后端装配的树形表），
 * 缺前序与技术特征；已确认的完整权要树在手时按权号补齐，使报告内的树也能展开全文。
 */
export function mergeClaimTreeDetail(
  base: ClaimTreeData,
  detail: ClaimTreeData | null | undefined,
): ClaimTreeData {
  if (!detail || detail.claims.length === 0) return base
  const byNo = new Map(detail.claims.map((c) => [c.no, c]))
  const claims = base.claims.map((claim) => {
    const rich = byNo.get(claim.no)
    if (!rich) return claim
    const needsDetail = claim.preamble.trim() === '' && claim.features.length === 0
    return {
      ...claim,
      preamble: needsDetail ? rich.preamble : claim.preamble,
      features: needsDetail ? rich.features : claim.features,
      refs: claim.refs.length > 0 ? claim.refs : rich.refs,
      delta: claim.delta || rich.delta,
    }
  })
  return {
    claims,
    ambiguities: base.ambiguities.length > 0 ? base.ambiguities : detail.ambiguities,
  }
}

/** 统计（第三节节导语：共 N 项 · 独立 x / 从属 y）。 */
export function claimTreeStats(claims: ClaimNode[]): {
  total: number
  independent: number
  dependent: number
} {
  const independent = claims.filter((c) => c.type === 'independent').length
  return { total: claims.length, independent, dependent: claims.length - independent }
}

// ---------------------------------------------------------------------------
// 解读报告 report.json
// ---------------------------------------------------------------------------

/**
 * 报告 meta（report_template.md「报告 meta JSON」，替代原 Obsidian frontmatter）。
 * 后端 `ReaderMeta` 字段名为 pub_no / type_label / applicants / ipc(list)，
 * 经 normalizeMeta 归一到此处的展示字段。
 */
export interface ReaderReportMeta {
  pub_number?: string
  title?: string
  patent_type?: string
  domain?: string
  /** 多个 IPC 分类号已合并为一行展示。 */
  ipc?: string
  assignees?: string[]
  pub_date?: string
  read_date?: string
  perspective?: string
  /** full_text | abstract_only | partial（展示时经 i18n 取中文标签）。 */
  evidence_scope?: string
  confidence_speculative?: boolean
}

/** 四种提示块 + 附图块（模板 callout → 站内 block 类型映射）。 */
export type ReaderCalloutKind = 'meta' | 'claim' | 'grounding' | 'warning' | 'figure'

export interface ReaderMarkdownBlock {
  type: 'markdown'
  markdown: string
}

export interface ReaderTableBlock {
  type: 'table'
  columns: string[]
  rows: string[][]
  caption?: string
}

export interface ReaderClaimTreeBlock {
  type: 'claim_tree'
  tree: ClaimTreeData
}

export interface ReaderMermaidBlock {
  type: 'mermaid'
  code: string
}

export interface ReaderCalloutBlock {
  type: 'callout'
  kind: ReaderCalloutKind
  title: string
  markdown: string
  /** `> [!warning]-` 默认折叠。 */
  collapsed: boolean
}

export type ReaderBlock =
  | ReaderMarkdownBlock
  | ReaderTableBlock
  | ReaderClaimTreeBlock
  | ReaderMermaidBlock
  | ReaderCalloutBlock

export interface ReaderSection {
  /** 目录锚点（稳定于同一份报告内）。 */
  id: string
  /** 节标题（`## 一、一句话`）；空串 = 不进目录的前言块。 */
  title: string
  blocks: ReaderBlock[]
}

export interface ReaderReport {
  meta: ReaderReportMeta
  sections: ReaderSection[]
}

/** `callout-patent-meta` / `callout-meta` / `patent-meta` → 'meta'。 */
export function normalizeCalloutKind(raw: string): ReaderCalloutKind {
  const key = raw.replace(/^callout[-_]?/, '').replace(/^patent[-_]?/, '').replace(/_/g, '-')
  if (key.startsWith('meta')) return 'meta'
  if (key.startsWith('claim')) return 'claim'
  if (key.startsWith('ground')) return 'grounding'
  if (key.startsWith('fig')) return 'figure'
  return 'warning'
}

function normalizeTableBlock(record: Record<string, unknown>): ReaderTableBlock {
  const columns = asArray(record.columns ?? record.headers ?? record.header).map(toStr)
  const rows = asArray(record.rows ?? record.data).map((row) =>
    Array.isArray(row)
      ? row.map(toStr)
      : columns.map((col) => toStr(asRecord(row)?.[col])),
  )
  return { type: 'table', columns, rows, caption: toStr(record.caption) || undefined }
}

/**
 * 单个原始 block → 规整 block（未知类型降级为 markdown）。
 *
 * 后端 `ReportBlock` 为 `{type, content}`，content 形态随 type 变化
 * （models/reader.py §五）：markdown / mermaid 是字符串；table 是
 * `{headers, rows, markdown}`；claim_tree 是 `{summary, rows, markdown}`；
 * callout-* 是 `{title, markdown}`。此处同时兼容字段平铺在 block 上的写法。
 */
function normalizeBlock(raw: unknown): ReaderBlock | null {
  const record = asRecord(raw)
  if (!record) {
    const text = toStr(raw)
    return text.trim() ? { type: 'markdown', markdown: text } : null
  }

  const type = toStr(record.type ?? record.kind) || 'markdown'
  const content = record.content
  const inner = asRecord(content)
  /** content 为对象时取其 markdown 字段，为字符串时即正文本身。 */
  const markdown = toStr(
    record.markdown ??
      record.text ??
      record.body ??
      (inner ? (inner.markdown ?? inner.text) : content),
  )

  if (type.startsWith('callout')) {
    return {
      type: 'callout',
      kind: normalizeCalloutKind(type),
      title: toStr(record.title ?? record.label ?? inner?.title),
      markdown,
      collapsed: record.collapsed === true || inner?.collapsed === true,
    }
  }
  if (type === 'mermaid') {
    return { type: 'mermaid', code: toStr(record.code ?? inner?.code ?? markdown) }
  }
  if (type === 'table') {
    const table = normalizeTableBlock(inner ?? record)
    if (table.columns.length > 0 || table.rows.length > 0) return table
    return markdown.trim() ? { type: 'markdown', markdown } : null
  }
  if (type === 'claim_tree' || type === 'claimTree') {
    const tree = normalizeClaimTree(
      record.tree ?? record.claim_tree ?? inner ?? record,
      record.deltas ?? inner?.deltas,
    )
    if (tree) return { type: 'claim_tree', tree }
    return markdown.trim() ? { type: 'markdown', markdown } : null
  }
  return markdown.trim() ? { type: 'markdown', markdown } : null
}

/** 字符串或字符串数组 → 一行展示文本（IPC 可能是 list）。 */
function toJoined(value: unknown, sep = '、'): string {
  if (Array.isArray(value)) return value.map(toStr).filter(Boolean).join(sep)
  return toStr(value)
}

function normalizeMeta(raw: unknown): ReaderReportMeta {
  const record = asRecord(raw)
  if (!record) return {}
  const assignees = [
    ...asArray(record.applicants).map(toStr),
    ...asArray(record.assignees).map(toStr),
  ].filter(Boolean)
  const meta: ReaderReportMeta = {
    pub_number: toStr(record.pub_no ?? record.pub_number ?? record.pubNumber) || undefined,
    title: toStr(record.title) || undefined,
    patent_type: toStr(record.type_label ?? record.patent_type) || undefined,
    domain: toStr(record.domain) || undefined,
    ipc: toJoined(record.ipc) || undefined,
    pub_date: toStr(record.pub_date) || undefined,
    read_date: toStr(record.read_date) || undefined,
    perspective: toStr(record.perspective) || undefined,
    evidence_scope: toStr(record.evidence_scope) || undefined,
    confidence_speculative: record.confidence_speculative === true,
  }
  if (assignees.length > 0) meta.assignees = Array.from(new Set(assignees))
  return meta
}

/**
 * report.json → 规整报告；无 sections 时返回 null（调用方回退到流式 markdown 解析）。
 * 兼容 `GET /reader/report` 的外层包装 `{report:{meta,sections}, markdown, …}`。
 */
export function normalizeReport(raw: unknown): ReaderReport | null {
  let record = asRecord(raw)
  if (!record) return null
  if (!Array.isArray(record.sections)) {
    const nested = asRecord(record.report)
    if (!nested) return null
    record = nested
  }
  const rawSections = asArray(record.sections)
  if (rawSections.length === 0) return null

  const sections: ReaderSection[] = []
  rawSections.forEach((item, index) => {
    const entry = asRecord(item)
    if (!entry) return
    const blocks = asArray(entry.blocks)
      .map(normalizeBlock)
      .filter((b): b is ReaderBlock => b !== null)
    const title = toStr(entry.title ?? entry.heading ?? entry.name)
    if (blocks.length === 0 && !title) return
    sections.push({
      id: toStr(entry.id ?? entry.anchor) || `sec-${index + 1}`,
      title,
      blocks,
    })
  })

  if (sections.length === 0) return null
  return { meta: normalizeMeta(record.meta), sections }
}

// ---------------------------------------------------------------------------
// 公开号
// ---------------------------------------------------------------------------

/** 宽松公开号 / 申请号校验：CN + 8–13 位数字（+ 可选校验位 / 种类码）。 */
const PUB_NO_RE = /^[A-Z]{2}\d{8,13}(\.\d)?[A-Z]?\d?$/

/** 归一：去空格与连字符、字母大写。 */
export function normalizePubNo(input: string): string {
  return input.replace(/[\s\-–—]/g, '').toUpperCase()
}

export function isLikelyPubNo(input: string): boolean {
  return PUB_NO_RE.test(normalizePubNo(input))
}

// ---------------------------------------------------------------------------
// react-query hooks
// ---------------------------------------------------------------------------

export const readerKeys = {
  report: (caseId: string) => ['case', caseId, 'reader-report'] as const,
  claimTree: (caseId: string) => ['case', caseId, 'reader-claim-tree'] as const,
}

/** 后端尚未实现该端点（404/405）→ 前端回退到流式 markdown 渲染。 */
export function isReaderEndpointMissing(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 404 || error.status === 405)
}

/** `GET /reader/report` 出参（api/reader.py ReaderReportOut）。 */
export interface ReaderReportBundle {
  report: ReaderReport | null
  /** 终稿 markdown（刷新后无 SSE 重放时的导出兜底）。 */
  markdown: string
  artifactId: string | null
  filename: string | null
  version: number | null
  /** 自检未决事项（不入正文，面板底部提示）。 */
  unresolved: string[]
}

/**
 * GET /cases/{id}/reader/report → {report, markdown, artifact_id, …}（C6 装配产物）。
 * 报告交付前后端返回 404，故 `enabled` 一般绑定「已产出版本 / 流水线已完成」。
 */
export function useReaderReport(caseId: string | undefined, enabled = true) {
  return useQuery<ReaderReportBundle>({
    queryKey: readerKeys.report(caseId ?? ''),
    queryFn: async () => {
      const raw = await api.get<unknown>(
        `/cases/${encodeURIComponent(caseId!)}/reader/report`,
      )
      const record = asRecord(raw) ?? {}
      return {
        report: normalizeReport(raw),
        markdown: toStr(record.markdown),
        artifactId: toStr(record.artifact_id) || null,
        filename: toStr(record.filename) || null,
        version: toNum(record.version),
        unresolved: asArray(record.unresolved).map(toStr).filter(Boolean),
      }
    },
    enabled: Boolean(caseId) && enabled,
    retry: false,
    staleTime: 30_000,
  })
}

/** GET /cases/{id}/reader/claim-tree → claim_tree.json（+ claim_deltas 合并）。 */
export function useReaderClaimTree(caseId: string | undefined, enabled = true) {
  return useQuery({
    queryKey: readerKeys.claimTree(caseId ?? ''),
    queryFn: async () => {
      const raw = await api.get<unknown>(
        `/cases/${encodeURIComponent(caseId!)}/reader/claim-tree`,
      )
      return normalizeClaimTree(raw)
    },
    enabled: Boolean(caseId) && enabled,
    retry: false,
    staleTime: 30_000,
  })
}
