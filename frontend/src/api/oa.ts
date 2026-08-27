/**
 * 案例库 API（backend-architecture.md §3 模块 D 端点表）：
 *
 * | GET    | /oa/library?tag=&defect_type=&status=&q=  | 列表 |
 * | POST   | /oa/library/ingest                        | 上传案例 PDF/MD → 草稿（multipart） |
 * | GET    | /oa/library/{id}                          | 详情（含笔记正文） |
 * | PUT    | /oa/library/{id}                          | 人审；{status:'confirmed'} → 切块 + 嵌入 |
 * | DELETE | /oa/library/{id}                          | 连带删 chunks + vec |
 * | POST   | /oa/library/rebuild                       | 全量重建 |
 * | GET    | /oa/library/search?q=&k=                   | 语义检索（必带 retrieval_mode） |
 *
 * 出参一律经本模块的宽松规整函数落到前端类型，缺字段降级为空值而非崩溃；
 * 端点尚未上线（404/405）时由页面用 `isOaLibraryMissing` 降级为空态。
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { API_BASE, ApiError, api } from '../lib/api'

// ---------------------------------------------------------------------------
// 类型（models/oa.py 的 API 契约段）
// ---------------------------------------------------------------------------

export type OaLibraryStatus = 'draft' | 'confirmed'
export type OaRetrievalMode = 'vector' | 'brute' | 'keyword'

/** 案例库 frontmatter（oa_case.schema.yaml；extra 允许，故保留原始字典）。 */
export type OaFrontmatter = Record<string, unknown>

export interface OaLibraryItem {
  id: string
  caseId: string
  title: string
  status: OaLibraryStatus
  embedded: boolean
  chunkCount: number
  patentType: string
  statutes: string[]
  defectTypes: string[]
  domain: string
  outcome: string
  tags: string[]
  frontmatter: OaFrontmatter
  createdAt: string
  updatedAt: string
  /** 仅检索态：本条命中的相似度（0–1）。 */
  score?: number
  /** 仅检索态：命中的笔记节选。 */
  chunkText?: string
}

export interface OaRedaction {
  originalHint: string
  replacedWith: string
  reason: string
}

export interface OaLibraryDetail extends OaLibraryItem {
  noteMd: string
  noteFullMd: string
  redactions: OaRedaction[]
  inferredFields: string[]
  chunks: string[]
  embedError: string
}

export interface OaLibraryListResult {
  items: OaLibraryItem[]
  total: number
}

export interface OaSearchResult {
  retrievalMode: OaRetrievalMode
  query: string
  k: number
  totalCandidates: number
  items: OaLibraryItem[]
  message: string
  error: string
}

export interface OaRebuildResult {
  ok: boolean
  queued: number
  dim: number
  mode: OaRetrievalMode
  vecAvailable: boolean
  message: string
}

export interface OaLibraryListParams {
  tag?: string
  defectType?: string
  status?: OaLibraryStatus | ''
  q?: string
  limit?: number
  offset?: number
}

// ---------------------------------------------------------------------------
// 规整
// ---------------------------------------------------------------------------

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

function toStr(value: unknown): string {
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return ''
}

function toNum(value: unknown, fallback = 0): number {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string' && value.trim() && Number.isFinite(Number(value))) {
    return Number(value)
  }
  return fallback
}

function toStrList(value: unknown): string[] {
  if (Array.isArray(value)) return value.map(toStr).filter(Boolean)
  const single = toStr(value)
  return single ? [single] : []
}

/** frontmatter 优先、行字段兜底地取一个字符串。 */
function fmStr(row: Record<string, unknown>, fm: OaFrontmatter, ...keys: string[]): string {
  for (const key of keys) {
    const direct = toStr(row[key])
    if (direct) return direct
    const inFm = toStr(fm[key])
    if (inFm) return inFm
  }
  return ''
}

function fmList(row: Record<string, unknown>, fm: OaFrontmatter, ...keys: string[]): string[] {
  for (const key of keys) {
    const direct = toStrList(row[key])
    if (direct.length > 0) return direct
    const inFm = toStrList(fm[key])
    if (inFm.length > 0) return inFm
  }
  return []
}

export function normalizeLibraryItem(raw: unknown): OaLibraryItem {
  const row = asRecord(raw) ?? {}
  const fm = asRecord(row.frontmatter) ?? {}
  const status = toStr(row.status) === 'confirmed' ? 'confirmed' : 'draft'
  return {
    id: toStr(row.id) || toStr(row.library_id) || toStr(fm.case_id),
    caseId: fmStr(row, fm, 'case_id'),
    title: fmStr(row, fm, 'title') || fmStr(row, fm, 'case_id') || '未命名案例',
    status,
    embedded: row.embedded === true || toNum(row.chunk_count) > 0,
    chunkCount: toNum(row.chunk_count),
    patentType: fmStr(row, fm, 'patent_type'),
    statutes: fmList(row, fm, 'statutes'),
    defectTypes: fmList(row, fm, 'defect_types'),
    domain: fmStr(row, fm, 'domain'),
    outcome: fmStr(row, fm, 'outcome'),
    tags: fmList(row, fm, 'tags'),
    frontmatter: fm,
    createdAt: fmStr(row, fm, 'created_at'),
    updatedAt: fmStr(row, fm, 'updated_at'),
  }
}

export function normalizeLibraryDetail(raw: unknown): OaLibraryDetail {
  const row = asRecord(raw) ?? {}
  const base = normalizeLibraryItem(raw)
  return {
    ...base,
    noteMd: toStr(row.note_md),
    noteFullMd: toStr(row.note_full_md) || toStr(row.note_md),
    redactions: (Array.isArray(row.redactions) ? row.redactions : []).map((item) => {
      const r = asRecord(item) ?? {}
      return {
        originalHint: toStr(r.original_hint),
        replacedWith: toStr(r.replaced_with),
        reason: toStr(r.reason),
      }
    }),
    inferredFields: toStrList(row.inferred_fields),
    chunks: toStrList(row.chunks),
    embedError: toStr(row.embed_error),
  }
}

/** 检索命中（OaSearchHit）→ 列表项（带 score / 节选）。 */
function normalizeHit(raw: unknown): OaLibraryItem {
  const row = asRecord(raw) ?? {}
  const fm = asRecord(row.frontmatter) ?? {}
  const item = normalizeLibraryItem({
    ...row,
    id: toStr(row.library_id) || toStr(row.id),
    frontmatter: fm,
    // 命中条目不带 status，按已入库处理（只有 confirmed 才会被嵌入检索）
    status: toStr(row.status) || 'confirmed',
  })
  return {
    ...item,
    score: toNum(row.score),
    chunkText: toStr(row.chunk_text),
  }
}

function normalizeMode(value: unknown): OaRetrievalMode {
  const mode = toStr(value)
  return mode === 'vector' || mode === 'brute' || mode === 'keyword' ? mode : 'keyword'
}

export function normalizeSearchResult(raw: unknown): OaSearchResult {
  const row = asRecord(raw) ?? {}
  const hits = Array.isArray(row.hits) ? row.hits : Array.isArray(row.items) ? row.items : []
  return {
    retrievalMode: normalizeMode(row.retrieval_mode),
    query: toStr(row.query),
    k: toNum(row.k, hits.length),
    totalCandidates: toNum(row.total_candidates, hits.length),
    items: hits.map(normalizeHit),
    message: toStr(row.message),
    error: toStr(row.error),
  }
}

function normalizeList(raw: unknown): OaLibraryListResult {
  if (Array.isArray(raw)) {
    const items = raw.map(normalizeLibraryItem)
    return { items, total: items.length }
  }
  const row = asRecord(raw) ?? {}
  const rawItems = Array.isArray(row.items) ? row.items : []
  const items = rawItems.map(normalizeLibraryItem)
  return { items, total: toNum(row.total, items.length) }
}

/** 案例库端点尚未上线（404/405）→ 页面降级为空态而非报错。 */
export function isOaLibraryMissing(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 404 || error.status === 405)
}

// ---------------------------------------------------------------------------
// react-query hooks
// ---------------------------------------------------------------------------

export const oaKeys = {
  root: ['oa-library'] as const,
  list: (params: OaLibraryListParams) => ['oa-library', 'list', params] as const,
  detail: (id: string) => ['oa-library', 'detail', id] as const,
  search: (q: string, k: number) => ['oa-library', 'search', q, k] as const,
}

/** GET /oa/library（tag / defect_type / status 过滤 + 关键词模糊）。 */
export function useOaLibraryList(params: OaLibraryListParams = {}) {
  return useQuery<OaLibraryListResult>({
    queryKey: oaKeys.list(params),
    queryFn: async () => {
      const raw = await api.get<unknown>('/oa/library', {
        query: {
          tag: params.tag,
          defect_type: params.defectType,
          status: params.status,
          q: params.q,
          limit: params.limit ?? 50,
          offset: params.offset ?? 0,
        },
      })
      return normalizeList(raw)
    },
    retry: false,
    staleTime: 10_000,
  })
}

/** GET /oa/library/{id}（抽屉详情：笔记正文 + frontmatter + 脱敏登记）。 */
export function useOaLibraryDetail(id: string | null, enabled = true) {
  return useQuery<OaLibraryDetail>({
    queryKey: oaKeys.detail(id ?? ''),
    queryFn: async () =>
      normalizeLibraryDetail(await api.get<unknown>(`/oa/library/${encodeURIComponent(id!)}`)),
    enabled: Boolean(id) && enabled,
    retry: false,
    staleTime: 10_000,
  })
}

/**
 * GET /oa/library/search（语义检索；`retrieval_mode` 必须在 UI 明示）。
 * `query` 为空时不发请求，页面回落普通列表。
 */
export function useOaLibrarySearch(query: string, k = 8, enabled = true) {
  const trimmed = query.trim()
  return useQuery<OaSearchResult>({
    queryKey: oaKeys.search(trimmed, k),
    queryFn: async () =>
      normalizeSearchResult(
        await api.get<unknown>('/oa/library/search', { query: { q: trimmed, k } }),
      ),
    enabled: enabled && trimmed.length > 0,
    retry: false,
    staleTime: 30_000,
  })
}

export interface OaIngestInput {
  files: File[]
  /** 人工补充说明（结案信息、背景等）。 */
  notes?: string
  /** 结案结果（OUTCOMES 之一，可留空由模型推断）。 */
  outcome?: string
}

/** POST /oa/library/ingest（multipart：files + notes + outcome）→ draft 案例。 */
export function useOaLibraryIngest() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (input: OaIngestInput) => {
      const form = new FormData()
      for (const file of input.files) form.append('files', file, file.name)
      if (input.notes) form.append('notes', input.notes)
      if (input.outcome) form.append('outcome', input.outcome)
      const raw = await api.post<unknown>('/oa/library/ingest', form)
      const row = asRecord(raw) ?? {}
      return normalizeLibraryDetail(row.item ?? raw)
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: oaKeys.root })
    },
  })
}

export interface OaLibraryUpdateInput {
  id: string
  status?: OaLibraryStatus
  noteMd?: string
  frontmatter?: OaFrontmatter
}

/** PUT /oa/library/{id}（status='confirmed' 触发切块 + 嵌入）。 */
export function useOaLibraryUpdate() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (input: OaLibraryUpdateInput) => {
      const body: Record<string, unknown> = {}
      if (input.status) body.status = input.status
      if (input.noteMd !== undefined) body.note_md = input.noteMd
      if (input.frontmatter !== undefined) body.frontmatter = input.frontmatter
      return normalizeLibraryDetail(
        await api.put<unknown>(`/oa/library/${encodeURIComponent(input.id)}`, body),
      )
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: oaKeys.root })
    },
  })
}

/** 语义化别名：人审通过 → 确认入库。 */
export function useOaLibraryConfirm() {
  const update = useOaLibraryUpdate()
  return {
    ...update,
    confirm: (id: string) => update.mutateAsync({ id, status: 'confirmed' }),
  }
}

/** DELETE /oa/library/{id}（连带删 chunks + vec）。 */
export function useOaLibraryDelete() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (id: string) => {
      await api.delete<unknown>(`/oa/library/${encodeURIComponent(id)}`)
      return id
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: oaKeys.root })
    },
  })
}

/** POST /oa/library/rebuild（全量重建，后台任务）。 */
export function useOaLibraryRebuild() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (): Promise<OaRebuildResult> => {
      const raw = await api.post<unknown>('/oa/library/rebuild')
      const row = asRecord(raw) ?? {}
      return {
        ok: row.ok !== false,
        queued: toNum(row.queued),
        dim: toNum(row.dim),
        mode: normalizeMode(row.mode),
        vecAvailable: row.vec_available === true,
        message: toStr(row.message),
      }
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: oaKeys.root })
    },
  })
}

/** 案例原始笔记下载地址（详情抽屉「下载笔记」）。 */
export function libraryNoteUrl(id: string): string {
  return `${API_BASE}/oa/library/${encodeURIComponent(id)}/note`
}
