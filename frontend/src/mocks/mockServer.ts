/**
 * Mock SSE 传输层 + Mock API 拦截（frontend-design.md §5 Mock 模式）。
 * - createMockSseTransport()：拦 GET /cases/{id}/events，回放 mockEvents 时间线
 *   （支持 Last-Event-ID 续传、AbortSignal 取消、速度因子调节、HITL 门控暂停）；
 * - installMockApi()：拦案件 / 消息 / 流水线 / 交付物 / p2p 内容 / 解读产物 /
 *   交底书迭代等 REST 端点，
 *   使 VITE_USE_MOCKS=1 下四模块工作台可脱离后端全流程走通；其余请求透传原生 fetch。
 */

import { API_BASE } from '../lib/api'
import type { SseTransport } from '../lib/sse'
import type { Artifact, Case, CaseDetail, Message, Module } from '../types/models'
import type { Quota, Role, UserOut, UserStatus } from '../api/auth'
import type { AuditEntry } from '../api/admin'
import {
  designSchemaScript,
  disclosureScript,
  mockScripts,
  oaLibraryItems,
  oaLibraryNotes,
  oaScript,
  paperPatentContent,
  paperScript,
  readerAmbiguities,
  readerClaims,
  readerDeltas,
  readerDeliveryFile,
  readerReport,
  readerReportMarkdown,
  readerScript,
  readerUnresolved,
  utilitySchemaScript,
  type MockGateKind,
  type MockScript,
} from './mockEvents'

// ---------------------------------------------------------------------------
// 回放速度
// ---------------------------------------------------------------------------

let speedFactor = 1

/** 设置回放速度因子（2 = 两倍速）。 */
export function setMockSpeed(factor: number): void {
  speedFactor = Number.isFinite(factor) && factor > 0 ? factor : 1
}

export function getMockSpeed(): number {
  return speedFactor
}

// ---------------------------------------------------------------------------
// 剧本选择
// ---------------------------------------------------------------------------

/**
 * 依 caseId 选剧本：含 "paper" → 论文转专利，含 "reader" → 专利解读，
 * 含 "oa" → 审查意见答复，否则交底书。
 */
export function pickScript(caseId: string): MockScript {
  if (caseId.includes('paper')) return paperScript
  if (caseId.includes('reader')) return readerScript
  if (caseId.includes('oa')) return oaScript
  // 交底书分支剧本（A3b schema_fill 填表与线稿）
  if (caseId.includes('design')) return designSchemaScript
  if (caseId.includes('utility') || caseId.includes('schema')) return utilitySchemaScript
  const hinted = mockScripts.find((s) => s.caseIdHint === caseId)
  return hinted ?? disclosureScript
}

/** 按 key 取剧本（页面显式指定回放哪条剧本时使用）。 */
export function getMockScript(key: MockScript['key']): MockScript {
  return mockScripts.find((s) => s.key === key) ?? disclosureScript
}

// ---------------------------------------------------------------------------
// HITL 门控：时间线在 interaction_required / 交付后暂停，
// 等待 POST /pipeline/input（卡片确认）或 POST /disclosure/iterate（迭代）
// ---------------------------------------------------------------------------

interface GateEntry {
  kind: MockGateKind
  release: () => void
}

const gateResolvers = new Map<string, GateEntry>()

/**
 * 门控放行：
 * - source='input'   → 放行 /pipeline/input 门控；
 * - source='iterate' → 放行 /disclosure/iterate 门控；
 * - source='force'   → 无视门控类型放行（/pipeline/cancel）。
 */
export function resolveMockGate(caseId: string, source: MockGateKind | 'force' = 'input'): void {
  const entry = gateResolvers.get(caseId)
  if (!entry) return
  if (source === 'force' || entry.kind === source) entry.release()
}

// ---------------------------------------------------------------------------
// Mock SSE transport
// ---------------------------------------------------------------------------

const EVENTS_URL_RE = /\/cases\/([^/?#]+)\/events/

function abortError(): DOMException {
  return new DOMException('SSE 连接已中止', 'AbortError')
}

/** 可被 signal 取消的 sleep；被取消时抛 AbortError。 */
function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(abortError())
      return
    }
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', onAbort)
      resolve()
    }, ms)
    const onAbort = () => {
      clearTimeout(timer)
      reject(abortError())
    }
    signal.addEventListener('abort', onAbort, { once: true })
  })
}

/** 等待用户回答门控；超时兜底自动放行，signal 中止则抛 AbortError。 */
function waitForGate(
  caseId: string,
  kind: MockGateKind,
  signal: AbortSignal,
  timeoutMs: number,
): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(abortError())
      return
    }
    const cleanup = () => {
      clearTimeout(timer)
      signal.removeEventListener('abort', onAbort)
      if (gateResolvers.get(caseId) === entry) gateResolvers.delete(caseId)
    }
    const release = () => {
      cleanup()
      resolve()
    }
    const onAbort = () => {
      cleanup()
      reject(abortError())
    }
    const timer = setTimeout(release, timeoutMs)
    const entry: GateEntry = { kind, release }
    signal.addEventListener('abort', onAbort, { once: true })
    gateResolvers.set(caseId, entry)
  })
}

export interface MockSseTransportOptions {
  /** 剧本覆盖（缺省依 caseId 从 url 推断）。 */
  script?: MockScript
  /** 时间线播完后的 ping 间隔 ms（默认 15000）。 */
  pingIntervalMs?: number
  /** 门控无人回答时的兜底放行时长 ms（默认 90000）。 */
  gateTimeoutMs?: number
}

/**
 * Mock 传输层：解析 url 中的 caseId → 选剧本 → 按 delay/速度逐事件回放。
 * 事件 id = 时间线序号（1 起），lastEventId 续传即跳过已放过的条目；
 * 标记 gate 的事件发出后暂停，直到 POST /pipeline/input 放行（或超时兜底）。
 * 回放结束后维持连接（周期 ping），直到 signal 中止。
 */
export function createMockSseTransport(options: MockSseTransportOptions = {}): SseTransport {
  const { script: scriptOverride, pingIntervalMs = 15_000, gateTimeoutMs = 90_000 } = options

  return async ({ url, lastEventId, signal, onOpen, onFrame }) => {
    const match = EVENTS_URL_RE.exec(url)
    if (!match) throw new Error(`Mock transport 无法识别事件流 URL：${url}`)
    const caseId = decodeURIComponent(match[1])
    const script = scriptOverride ?? pickScript(caseId)

    // 模拟连接建立耗时
    await sleep(120, signal)
    onOpen()

    // Last-Event-ID = 已收到的最后一条时间线序号 → 从其后续播
    const startIndex = lastEventId ? Number.parseInt(lastEventId, 10) || 0 : 0
    for (let i = startIndex; i < script.timeline.length; i += 1) {
      const item = script.timeline[i]
      await sleep(item.delay / speedFactor, signal)
      onFrame({
        event: item.event,
        data: JSON.stringify(item.data),
        id: String(i + 1),
      })
      if (item.gate) {
        const kind: MockGateKind = item.gate === true ? 'input' : item.gate
        await waitForGate(caseId, kind, signal, item.gateTimeoutMs ?? gateTimeoutMs)
      }
    }

    // 播完后保持连接：周期 ping，直到中止
    for (;;) {
      await sleep(pingIntervalMs, signal)
      onFrame({
        event: 'ping',
        data: JSON.stringify({ t: new Date().toISOString() }),
      })
    }
  }
}

// ---------------------------------------------------------------------------
// Mock API（fetch 拦截）
// ---------------------------------------------------------------------------

let restoreFetch: (() => void) | null = null
let mockCaseSeq = 0
let mockFileSeq = 0
let mockLibrarySeq = 0

/** 案例库行（mock 内可变副本；PUT/DELETE/ingest 直接改这份内存表）。 */
interface MockLibraryRow {
  id: string
  case_id: string
  title: string
  status: string
  embedded: boolean
  chunk_count: number
  patent_type: string
  statutes: string[]
  defect_types: string[]
  domain: string
  outcome: string
  tags: string[]
  frontmatter: Record<string, unknown>
  created_at: string
  updated_at: string
}

let oaLibrary: MockLibraryRow[] = oaLibraryItems.map((row) => ({
  ...row,
  statutes: [...row.statutes],
  defect_types: [...row.defect_types],
  tags: [...row.tags],
  frontmatter: { ...row.frontmatter },
}))

function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function nowIso(): string {
  return new Date().toISOString()
}

/** caseId 猜模块（mock 下仅用于案件详情展示）。 */
function guessModule(caseId: string): Module {
  if (caseId.includes('paper')) return 'paper2patent'
  if (caseId.includes('reader')) return 'reader'
  if (caseId.includes('oa')) return 'oa'
  return 'disclosure'
}

function mockCase(module: Module, title: string | null | undefined, id?: string): Case {
  mockCaseSeq += 1
  const now = nowIso()
  return {
    // `new-` 前缀：不能和 seedMockCases 的 demo-<module>-N 撞号，
    // 否则新建的案件与预置案件同 id，侧栏同一分组里出现两个相同 key。
    id: id ?? `new-${module}-${mockCaseSeq}-${Date.now().toString(36)}`,
    module,
    title: (title ?? '').trim() || '未命名案件',
    patent_type: null,
    status: 'draft',
    current_step: null,
    contact: null,
    created_at: now,
    updated_at: now,
  }
}

/**
 * 侧栏演示会话（GET /cases）。
 * 空列表会让 mock 模式下的侧栏（分组计数 / 搜索 / 条目 … 菜单 / 待确认红点）
 * 全部无从触发，四个模块的剧本也只能靠手敲 URL 进入；这里给每个模块各铺两条，
 * 其 id 命中 pickScript 的关键字，点开即回放对应剧本。
 */
function seedMockCases(): Case[] {
  const rows: [string, Module, string, Case['status']][] = [
    ['demo-disclosure-1', 'disclosure', '便携式术后康复监测装置', 'waiting_user'],
    ['demo-disclosure-2', 'disclosure', '内窥镜镜头防雾结构', 'draft'],
    ['demo-paper-1', 'paper2patent', '图神经网络病灶分割（论文转专利）', 'running'],
    ['demo-paper-2', 'paper2patent', '多模态检索排序论文', 'draft'],
    ['demo-reader-1', 'reader', 'CN117994321A 冠脉狭窄评估解读', 'completed'],
    ['demo-reader-2', 'reader', 'CN115774321A 插补装置解读', 'draft'],
    ['demo-oa-1', 'oa', 'CN202410123456.7 一通答复', 'waiting_user'],
    ['demo-oa-2', 'oa', 'CN202311998877.1 二通答复', 'draft'],
  ]
  return rows.map(([id, module, title, status], i) => {
    const ts = new Date(Date.now() - i * 3_600_000).toISOString()
    return {
      id,
      module,
      title,
      patent_type: null,
      status,
      current_step: null,
      contact: null,
      created_at: ts,
      updated_at: ts,
    }
  })
}

let mockCases: Case[] = seedMockCases()

// ---------------------------------------------------------------------------
// Mock 账号系统（M8：登录 / 会话 / 管理后台）
// ---------------------------------------------------------------------------

/** 会话持久化到 localStorage，刷新页面后仍保持登录（真实后端靠 Cookie）。 */
const MOCK_SESSION_KEY = 'pa-mock-session'
const MOCK_CSRF = 'mock-csrf-token-2026'

function quota(
  daily_llm_calls = 0,
  monthly_tokens = 0,
  storage_mb = 0,
  max_cases = 0,
): Quota {
  return { daily_llm_calls, monthly_tokens, storage_mb, max_cases }
}

function makeMockUser(
  id: string,
  username: string,
  display_name: string,
  role: Role,
  extra: Partial<UserOut> = {},
): UserOut {
  const now = nowIso()
  return {
    id,
    username,
    display_name,
    role,
    status: 'active',
    must_change_password: false,
    last_login_at: null,
    locked_until: null,
    quota: quota(),
    usage: {
      llm_calls_today: 0,
      tokens_this_month: 0,
      storage_mb: 0,
      case_count: 0,
    },
    created_at: now,
    updated_at: now,
    ...extra,
  }
}

/** 案件 → 模型档位（mock 内存态：换档后详情接口要能回出新值，否则开关会弹回去）。 */
const mockCaseTiers: Record<string, 'fast' | 'deep'> = {}

let mockUsers: UserOut[] = [
  makeMockUser('u-admin', 'admin', '平台管理员', 'admin', {
    last_login_at: new Date(Date.now() - 3_600_000).toISOString(),
    usage: { llm_calls_today: 12, tokens_this_month: 184_320, storage_mb: 42.7, case_count: 2 },
  }),
  makeMockUser('u-alice', 'alice', '李静（专利工程师）', 'user', {
    last_login_at: new Date(Date.now() - 7_200_000).toISOString(),
    quota: quota(200, 2_000_000, 2048, 50),
    usage: { llm_calls_today: 37, tokens_this_month: 912_004, storage_mb: 318.4, case_count: 4 },
  }),
  makeMockUser('u-bob', 'bob', '王工', 'user', {
    last_login_at: new Date(Date.now() - 3 * 86_400_000).toISOString(),
    quota: quota(100, 1_000_000, 1024, 20),
    usage: { llm_calls_today: 0, tokens_this_month: 26_800, storage_mb: 64.1, case_count: 2 },
  }),
  makeMockUser('u-newbie', 'newbie', '新同事', 'user', {
    must_change_password: true,
    quota: quota(50, 500_000, 512, 10),
    usage: { llm_calls_today: 0, tokens_this_month: 0, storage_mb: 0, case_count: 0 },
  }),
  makeMockUser('u-frozen', 'frozen', '离职账号', 'user', {
    status: 'disabled',
    last_login_at: new Date(Date.now() - 30 * 86_400_000).toISOString(),
    usage: { llm_calls_today: 0, tokens_this_month: 0, storage_mb: 12.5, case_count: 1 },
  }),
]

/** 案件属主（用于跨用户提示条与「查看其案件」）。 */
const mockCaseOwners: Record<string, string> = {
  'demo-disclosure-1': 'u-alice',
  'demo-disclosure-2': 'u-alice',
  'demo-paper-1': 'u-alice',
  'demo-paper-2': 'u-bob',
  'demo-reader-1': 'u-bob',
  'demo-reader-2': 'u-admin',
  'demo-oa-1': 'u-alice',
  'demo-oa-2': 'u-admin',
}

let mockAuditSeq = 100
let mockAudit: AuditEntry[] = [
  {
    id: (mockAuditSeq -= 1),
    actor_id: 'u-admin',
    actor_name: 'admin',
    action: 'cross_user_read',
    target_type: 'case',
    target_id: 'demo-oa-1',
    target_owner: 'u-alice',
    detail: { module: 'oa', title: 'CN202410123456.7 一通答复' },
    ip: '10.0.0.8',
    created_at: new Date(Date.now() - 600_000).toISOString(),
  },
  {
    id: (mockAuditSeq -= 1),
    actor_id: 'u-alice',
    actor_name: 'alice',
    action: 'login',
    target_type: null,
    target_id: null,
    target_owner: null,
    detail: {},
    ip: '10.0.0.31',
    created_at: new Date(Date.now() - 7_200_000).toISOString(),
  },
  {
    id: (mockAuditSeq -= 1),
    actor_id: null,
    actor_name: 'bob',
    action: 'login_failed',
    target_type: null,
    target_id: null,
    target_owner: null,
    detail: { code: 'invalid_credentials', reason: '用户名或密码错误' },
    ip: '10.0.0.44',
    created_at: new Date(Date.now() - 9_000_000).toISOString(),
  },
  {
    id: (mockAuditSeq -= 1),
    actor_id: 'u-admin',
    actor_name: 'admin',
    action: 'user_create',
    target_type: 'user',
    target_id: 'u-newbie',
    target_owner: null,
    detail: { username: 'newbie', role: 'user' },
    ip: '10.0.0.8',
    created_at: new Date(Date.now() - 86_400_000).toISOString(),
  },
  {
    id: (mockAuditSeq -= 1),
    actor_id: 'u-admin',
    actor_name: 'admin',
    action: 'settings_update',
    target_type: 'settings',
    target_id: 'llm',
    target_owner: null,
    detail: { model: 'deepseek-chat', base_url: 'https://api.deepseek.com/v1' },
    ip: '10.0.0.8',
    created_at: new Date(Date.now() - 2 * 86_400_000).toISOString(),
  },
]

let mockUserSeq = 0

function readMockSession(): string | null {
  try {
    return localStorage.getItem(MOCK_SESSION_KEY)
  } catch {
    return null
  }
}

function writeMockSession(userId: string | null): void {
  try {
    if (userId) localStorage.setItem(MOCK_SESSION_KEY, userId)
    else localStorage.removeItem(MOCK_SESSION_KEY)
  } catch {
    /* ignore */
  }
  // 真实后端把 CSRF token 写进非 httpOnly Cookie，这里照做，好让 lib/api 的
  // X-CSRF-Token 逻辑在 mock 模式下也走真实路径
  try {
    document.cookie = userId
      ? `pa_csrf=${MOCK_CSRF}; path=/; SameSite=Lax`
      : 'pa_csrf=; path=/; Max-Age=0'
  } catch {
    /* ignore */
  }
}

function currentMockUser(): UserOut | null {
  const id = readMockSession()
  if (!id) return null
  return mockUsers.find((u) => u.id === id) ?? null
}

function pushAudit(entry: Partial<AuditEntry> & { action: string }): void {
  mockAuditSeq += 1
  mockAudit = [
    {
      id: mockAuditSeq,
      actor_id: entry.actor_id ?? currentMockUser()?.id ?? null,
      actor_name: entry.actor_name ?? currentMockUser()?.username ?? null,
      action: entry.action,
      target_type: entry.target_type ?? null,
      target_id: entry.target_id ?? null,
      target_owner: entry.target_owner ?? null,
      detail: entry.detail ?? {},
      ip: entry.ip ?? '127.0.0.1',
      created_at: nowIso(),
    },
    ...mockAudit,
  ]
}

/** 随机可读密码（与后端 generate_password 的观感一致）。 */
function mockPassword(): string {
  const alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789'
  let out = ''
  for (let i = 0; i < 16; i += 1) {
    out += alphabet[Math.floor(Math.random() * alphabet.length)]
  }
  return out
}

function unauthorized(detail = '尚未登录或登录已过期，请重新登录'): Response {
  return jsonResponse({ detail }, 401)
}

/**
 * Mock 登录规则（便于 QA 覆盖各分支，不做真实密码校验）：
 * - 用户名须存在；密码 `bad` → 凭据错误；密码 `locked` → 锁定 5 分钟（触发倒计时）；
 * - 停用账号 → 明确提示；其余任意密码即登录成功。
 */
function mockLogin(username: string, password: string): Response {
  const user = mockUsers.find((u) => u.username === username.trim().toLowerCase())
  if (!user || password === 'bad') {
    pushAudit({
      action: 'login_failed',
      actor_id: null,
      actor_name: username,
      detail: { code: 'invalid_credentials' },
    })
    return jsonResponse({ detail: '用户名或密码错误' }, 401)
  }
  if (password === 'locked') {
    return jsonResponse(
      { detail: '账号已被临时锁定，请 5 分钟后再试；如需立即解锁请联系管理员。' },
      401,
    )
  }
  if (user.status !== 'active') {
    return jsonResponse({ detail: '账号已被停用，请联系管理员。' }, 401)
  }
  const next: UserOut = { ...user, last_login_at: nowIso() }
  mockUsers = mockUsers.map((u) => (u.id === user.id ? next : u))
  writeMockSession(user.id)
  pushAudit({ action: 'login', actor_id: user.id, actor_name: user.username })
  return jsonResponse({ user: next, csrf_token: MOCK_CSRF }, 200)
}

function paged<T>(items: T[], limit: number, offset: number) {
  return { items: items.slice(offset, offset + limit), total: items.length, limit, offset }
}

async function readJsonBody(input: RequestInfo | URL, init?: RequestInit): Promise<unknown> {
  try {
    if (typeof init?.body === 'string') return JSON.parse(init.body)
    if (input instanceof Request) return await input.clone().json()
  } catch {
    /* 忽略解析失败 */
  }
  return null
}

function requestUrl(input: RequestInfo | URL): string {
  if (typeof input === 'string') return input
  if (input instanceof URL) return input.toString()
  return input.url
}

function requestMethod(input: RequestInfo | URL, init?: RequestInit): string {
  if (init?.method) return init.method.toUpperCase()
  if (input instanceof Request) return input.method.toUpperCase()
  return 'GET'
}

/** 取请求头（init.headers 与 Request 两种写法都要认）。 */
function requestHeader(input: RequestInfo | URL, init: RequestInit | undefined, name: string): string {
  const fromInit = init?.headers ? new Headers(init.headers).get(name) : null
  if (fromInit) return fromInit
  if (input instanceof Request) return input.headers.get(name) ?? ''
  return ''
}

/** 取出 multipart 里的文件名（拿不到时用占位名）。 */
async function readUploadNames(input: RequestInfo | URL, init?: RequestInit): Promise<string[]> {
  try {
    const body = init?.body ?? (input instanceof Request ? await input.clone().formData() : null)
    if (body instanceof FormData) {
      return body
        .getAll('files')
        .map((v) => (v instanceof File ? v.name : String(v)))
        .filter(Boolean)
    }
  } catch {
    /* 忽略 */
  }
  return ['论文.pdf']
}

/**
 * 安装 Mock API 拦截（幂等，可重复调用）。返回卸载函数。
 * 覆盖：GET/POST /cases、GET /cases/{id}、/messages、/artifacts、
 * /pipeline/state、POST /pipeline/{start,input,resume,retry,cancel}、
 * POST /cases/{id}/files、GET|PUT /cases/{id}/p2p/content、
 * POST /cases/{id}/disclosure/iterate（放行交付后的迭代门控）。其余透传。
 */
export function installMockApi(): () => void {
  if (restoreFetch) return restoreFetch

  const originalFetch = window.fetch.bind(window)
  const casesUrl = `${API_BASE}/cases`
  const caseIdRe = new RegExp(`${API_BASE}/cases/([^/?#]+)`)
  const detailRe = new RegExp(`${API_BASE}/cases/[^/]+$`)
  const messagesRe = new RegExp(`${API_BASE}/cases/[^/]+/messages$`)
  const artifactsRe = new RegExp(`${API_BASE}/cases/[^/]+/artifacts$`)
  const filesRe = new RegExp(`${API_BASE}/cases/[^/]+/files$`)
  const stateRe = new RegExp(`${API_BASE}/cases/[^/]+/pipeline/state$`)
  const inputRe = new RegExp(`${API_BASE}/cases/[^/]+/pipeline/input$`)
  const commandRe = new RegExp(
    `${API_BASE}/cases/[^/]+/pipeline/(start|resume|retry|cancel)$`,
  )
  const p2pContentRe = new RegExp(`${API_BASE}/cases/[^/]+/p2p/content$`)
  const iterateRe = new RegExp(`${API_BASE}/cases/[^/]+/disclosure/iterate$`)
  const readerReportRe = new RegExp(`${API_BASE}/cases/[^/]+/reader/report$`)
  const readerTreeRe = new RegExp(`${API_BASE}/cases/[^/]+/reader/claim-tree$`)
  // /oa/library/{id}（search / ingest / rebuild 三个保留词在前面单独拦截）
  const oaLibraryIdRe = new RegExp(`${API_BASE}/oa/library/(?!search$|ingest$|rebuild$)([^/?#]+)$`)

  let p2pContent: unknown = paperPatentContent

  const adminUserIdRe = new RegExp(
    `${API_BASE}/admin/users/(?!$)([^/?#]+)(/cases|/reset-password)?$`,
  )

  window.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = requestUrl(input)
    const method = requestMethod(input, init)
    const path = url.split('?')[0]
    const caseId = decodeURIComponent(caseIdRe.exec(path)?.[1] ?? '')
    const params = new URL(url, window.location.origin).searchParams

    // --- 认证（M8） --------------------------------------------------------
    if (path.endsWith(`${API_BASE}/auth/login`) && method === 'POST') {
      const body = (await readJsonBody(input, init)) as {
        username?: string
        password?: string
      } | null
      return mockLogin(body?.username ?? '', body?.password ?? '')
    }
    if (path.endsWith(`${API_BASE}/auth/logout`) && method === 'POST') {
      const who = currentMockUser()
      if (who) pushAudit({ action: 'logout', actor_id: who.id, actor_name: who.username })
      writeMockSession(null)
      return jsonResponse({ ok: true }, 200)
    }
    if (path.endsWith(`${API_BASE}/auth/me`) && method === 'GET') {
      const me = currentMockUser()
      return me ? jsonResponse(me, 200) : unauthorized()
    }
    if (path.endsWith(`${API_BASE}/auth/change-password`) && method === 'POST') {
      const me = currentMockUser()
      if (!me) return unauthorized()
      const body = (await readJsonBody(input, init)) as {
        old_password?: string
        new_password?: string
      } | null
      if (body?.old_password === body?.new_password) {
        return jsonResponse({ detail: '新密码不能与当前密码相同' }, 400)
      }
      if ((body?.old_password ?? '') === 'bad') {
        return jsonResponse({ detail: '当前密码不正确' }, 400)
      }
      mockUsers = mockUsers.map((u) =>
        u.id === me.id ? { ...u, must_change_password: false, updated_at: nowIso() } : u,
      )
      pushAudit({ action: 'password_change', actor_id: me.id, actor_name: me.username })
      return jsonResponse({ ok: true, message: '密码已修改，其它设备上的登录已退出' }, 200)
    }

    /*
     * 认证闸门：除登录外，mock 也照着真实中间件的顺序拦一遍。
     * 不这样做，未登录状态下 mock 会照常回数据，路由守卫永远跳不到登录页，
     * 登录 / 改密 / 权限三条链路在 mock 模式下就完全测不了。
     */
    const me = currentMockUser()
    if (path.startsWith(API_BASE)) {
      if (!me) return unauthorized()
      /*
       * CSRF 双提交，照着后端中间件一并校验。
       * mock 放行缺头请求的话，「哪个写操作忘了带 X-CSRF-Token」这类问题
       * 要等接了真后端才暴露；就地 403 能让它在 mock 阶段立刻现形。
       */
      if (method !== 'GET' && method !== 'HEAD' && method !== 'OPTIONS') {
        if (requestHeader(input, init, 'X-CSRF-Token') !== MOCK_CSRF) {
          console.warn(`[mock] 缺少或错误的 X-CSRF-Token：${method} ${path}`)
          return jsonResponse({ detail: 'CSRF 校验失败，请刷新页面后重试' }, 403)
        }
      }
      if (
        me.must_change_password &&
        !path.endsWith(`${API_BASE}/auth/change-password`) &&
        !path.endsWith(`${API_BASE}/auth/logout`) &&
        !path.endsWith(`${API_BASE}/auth/me`)
      ) {
        return jsonResponse(
          { detail: '首次登录需要先修改密码', code: 'must_change_password' },
          403,
        )
      }
      if (path.includes(`${API_BASE}/admin/`) || path.endsWith(`${API_BASE}/admin`)) {
        if (me.role !== 'admin') return jsonResponse({ detail: '需要管理员权限' }, 403)
      }
    }

    // --- 管理后台（M8） ----------------------------------------------------
    if (path.endsWith(`${API_BASE}/admin/stats`) && method === 'GET') {
      const byModule: Record<string, number> = {}
      for (const c of mockCases) byModule[c.module] = (byModule[c.module] ?? 0) + 1
      return jsonResponse(
        {
          user_count: mockUsers.length,
          active_user_count: mockUsers.filter((u) => u.status === 'active').length,
          case_count: mockCases.length,
          case_count_by_module: byModule,
          llm_calls_today: 49,
          tokens_this_month: 1_123_124,
          storage_mb: 437.7,
          artifact_count: 126,
        },
        200,
      )
    }
    if (path.endsWith(`${API_BASE}/admin/audit`) && method === 'GET') {
      const action = params.get('action') ?? ''
      const actorId = params.get('actor_id') ?? ''
      const rows = mockAudit.filter(
        (e) => (!action || e.action === action) && (!actorId || e.actor_id === actorId),
      )
      return jsonResponse(
        paged(rows, Number(params.get('limit') ?? 100) || 100, Number(params.get('offset') ?? 0) || 0),
        200,
      )
    }
    if (path.endsWith(`${API_BASE}/admin/users`)) {
      if (method === 'GET') {
        const q = (params.get('q') ?? '').trim().toLowerCase()
        const role = params.get('role') ?? ''
        const status = params.get('status') ?? ''
        const rows = mockUsers.filter((u) => {
          if (role && u.role !== role) return false
          if (status && u.status !== status) return false
          if (q && !`${u.username} ${u.display_name}`.toLowerCase().includes(q)) return false
          return true
        })
        return jsonResponse(
          paged(rows, Number(params.get('limit') ?? 50) || 50, Number(params.get('offset') ?? 0) || 0),
          200,
        )
      }
      if (method === 'POST') {
        const body = (await readJsonBody(input, init)) as {
          username?: string
          display_name?: string
          role?: Role
          password?: string
          quota?: Quota
        } | null
        const username = (body?.username ?? '').trim().toLowerCase()
        if (mockUsers.some((u) => u.username === username)) {
          return jsonResponse({ detail: `用户名「${username}」已存在` }, 409)
        }
        mockUserSeq += 1
        const created = makeMockUser(
          `u-new-${mockUserSeq}`,
          username,
          body?.display_name ?? '',
          body?.role ?? 'user',
          { must_change_password: true, quota: body?.quota ?? quota() },
        )
        mockUsers = [created, ...mockUsers]
        pushAudit({
          action: 'user_create',
          target_type: 'user',
          target_id: created.id,
          detail: { username: created.username, role: created.role },
        })
        return jsonResponse(
          {
            user: created,
            password: body?.password || mockPassword(),
            note: '该密码仅显示这一次，请立即复制并交给用户；用户首次登录后必须修改。',
          },
          201,
        )
      }
    }
    const adminUserMatch = adminUserIdRe.exec(path)
    if (adminUserMatch) {
      const userId = decodeURIComponent(adminUserMatch[1])
      const suffix = adminUserMatch[2] ?? ''
      const index = mockUsers.findIndex((u) => u.id === userId)
      if (index < 0) return jsonResponse({ detail: '用户不存在' }, 404)
      const target = mockUsers[index]

      if (suffix === '/cases' && method === 'GET') {
        const rows = mockCases.filter((c) => (mockCaseOwners[c.id] ?? 'u-alice') === userId)
        return jsonResponse(paged(rows, 200, 0), 200)
      }
      if (suffix === '/reset-password' && method === 'POST') {
        const updated: UserOut = { ...target, must_change_password: true, updated_at: nowIso() }
        mockUsers = mockUsers.map((u, i) => (i === index ? updated : u))
        pushAudit({
          action: 'password_reset',
          target_type: 'user',
          target_id: userId,
          detail: { username: target.username },
        })
        return jsonResponse(
          {
            user: updated,
            password: mockPassword(),
            note: '该密码仅显示这一次，请立即复制并交给用户；用户首次登录后必须修改。',
          },
          200,
        )
      }
      if (!suffix && method === 'GET') return jsonResponse(target, 200)
      if (!suffix && method === 'PATCH') {
        const body = (await readJsonBody(input, init)) as {
          display_name?: string
          role?: Role
          status?: UserStatus
          quota?: Quota
        } | null
        const updated: UserOut = {
          ...target,
          display_name: body?.display_name ?? target.display_name,
          role: body?.role ?? target.role,
          status: body?.status ?? target.status,
          quota: body?.quota ?? target.quota,
          updated_at: nowIso(),
        }
        mockUsers = mockUsers.map((u, i) => (i === index ? updated : u))
        pushAudit({
          action: 'user_update',
          target_type: 'user',
          target_id: userId,
          detail: (body ?? {}) as Record<string, unknown>,
        })
        return jsonResponse(updated, 200)
      }
      if (!suffix && method === 'DELETE') {
        mockUsers = mockUsers.filter((u) => u.id !== userId)
        pushAudit({
          action: 'user_delete',
          target_type: 'user',
          target_id: userId,
          detail: { username: target.username, cases: 0 },
        })
        return jsonResponse(
          { ok: true, message: `已删除用户「${target.username}」及其 0 个案件` },
          200,
        )
      }
    }

    // --- 平台设置（mock 下只读回显，绝不 PUT 到真实后端覆盖用户配置） --------
    if (path.endsWith(`${API_BASE}/settings/llm`)) {
      const llm = {
        base_url: 'https://api.deepseek.com/v1',
        api_key: 'sk-***f3a9',
        model: 'deepseek-chat',
        temperature: 0.3,
        max_output_tokens: 8192,
        context_window: 65536,
        supports_json_mode: true,
      }
      if (method === 'GET' || method === 'PUT') return jsonResponse(llm, 200)
    }
    if (/\/settings\/model-tiers\/[^/]+\/test$/.test(path) && method === 'POST') {
      return jsonResponse({ ok: true, model: 'mock', latency_ms: 412 }, 200)
    }
    if (path.endsWith(`${API_BASE}/settings/model-tiers`)) {
      // 两档指向不同模型，聊天框上的档位开关才会渲染（同模型时它会自己隐藏）
      const tiers = {
        fast: {
          model: 'deepseek-chat',
          label: '',
          base_url: '',
          api_key: '',
          temperature: null,
          max_output_tokens: null,
          context_window: null,
          supports_json_mode: null,
        },
        // 深度档演示跨供应商：自己的地址 + 自己的密钥（读接口回掩码）
        deep: {
          model: 'claude-opus-5',
          label: '',
          base_url: 'https://api.anthropic.example/v1',
          api_key: 'sk-***9c41',
          temperature: null,
          max_output_tokens: 32768,
          context_window: null,
          supports_json_mode: null,
        },
        default_tier: 'deep',
        base_model: 'deepseek-chat',
        base_url: 'https://api.deepseek.com/v1',
        effective: { fast: 'deepseek-chat', deep: 'claude-opus-5' },
        effective_base_url: {
          fast: 'https://api.deepseek.com/v1',
          deep: 'https://api.anthropic.example/v1',
        },
      }
      if (method === 'GET' || method === 'PUT') return jsonResponse(tiers, 200)
    }
    if (path.endsWith(`${API_BASE}/settings/embedding`)) {
      const embedding = {
        enabled: true,
        provider: 'zhipu',
        base_url: 'https://open.bigmodel.cn/api/paas/v4',
        api_key: 'sk-***12cd',
        model: 'embedding-3',
        dim: 1024,
      }
      if (method === 'GET') return jsonResponse(embedding, 200)
      if (method === 'PUT') return jsonResponse({ settings: embedding, need_rebuild: false }, 200)
    }
    if (path.endsWith(`${API_BASE}/settings/image-gen`)) {
      const imageGen = {
        enabled: false,
        provider: 'zhipu',
        base_url: 'https://open.bigmodel.cn/api/paas/v4',
        api_key: '',
        model: 'cogview-3',
        size: '1024x1024',
      }
      if (method === 'GET' || method === 'PUT') return jsonResponse(imageGen, 200)
    }
    if (path.endsWith(`${API_BASE}/system/env`) && method === 'GET') {
      return jsonResponse(
        {
          python: { version: '3.12.4', executable: 'python', platform: 'Windows-11' },
          word: { available: true, path: 'WINWORD.EXE' },
          soffice: { available: false, path: null },
          chrome: { available: true, path: 'chrome.exe' },
          edge: { available: true, path: 'msedge.exe' },
          sqlite_vec: { loaded: true, error: null, version: 'v0.1.6' },
          playwright: { importable: true },
          data_dir: { path: 'C:\\PatentAgent\\data', disk: { total_gb: 476, free_gb: 128 } },
          frontend_dist: { available: true },
        },
        200,
      )
    }

    // --- 案件 -----------------------------------------------------------
    if (path.endsWith(casesUrl)) {
      if (method === 'POST') {
        const body = (await readJsonBody(input, init)) as {
          module?: Module
          title?: string | null
        } | null
        const created = mockCase(body?.module ?? 'disclosure', body?.title)
        mockCases = [created, ...mockCases]
        return jsonResponse(created, 201)
      }
      if (method === 'GET') {
        const wanted = params.get('module')
        // 数据隔离：普通用户只看自己的案件，管理员可见全部（与后端 owned_case_ids_clause 一致）
        const visible =
          me && me.role !== 'admin'
            ? mockCases.filter((c) => (mockCaseOwners[c.id] ?? me.id) === me.id)
            : mockCases
        const items = wanted ? visible.filter((c) => c.module === wanted) : visible
        return jsonResponse({ items, total: items.length, limit: 100, offset: 0 }, 200)
      }
    }
    // 改名 / 删除：必须在此拦下。透传会打到真实后端，
    // mock 模式误删真案件（侧栏 … 菜单的删除是不可撤销的）。
    if (detailRe.test(path) && (method === 'PATCH' || method === 'DELETE')) {
      const index = mockCases.findIndex((c) => c.id === caseId)
      if (method === 'DELETE') {
        mockCases = mockCases.filter((c) => c.id !== caseId)
        return new Response(null, { status: 204 })
      }
      const body = (await readJsonBody(input, init)) as {
        title?: string
        model_tier?: 'fast' | 'deep'
      } | null
      const current = mockCases[index] ?? mockCase(guessModule(caseId), null, caseId)
      if (body?.model_tier) mockCaseTiers[caseId] = body.model_tier
      const next: Case = {
        ...current,
        title: (body?.title ?? current.title).trim() || current.title,
        updated_at: nowIso(),
      }
      mockCases = index >= 0 ? mockCases.map((c, i) => (i === index ? next : c)) : [next, ...mockCases]
      return jsonResponse(next, 200)
    }
    if (method === 'GET' && detailRe.test(path)) {
      const known = mockCases.find((c) => c.id === caseId)
      const owner = mockCaseOwners[caseId] ?? me?.id ?? 'u-alice'
      // 普通用户命中他人案件 → 404（与后端 resolve_case 一致，不泄露案件是否存在）
      if (me && me.role !== 'admin' && owner !== me.id) {
        return jsonResponse({ detail: '案件不存在' }, 404)
      }
      const detail: CaseDetail = {
        ...(known ?? mockCase(guessModule(caseId), null, caseId)),
        state: { _model_tier: mockCaseTiers[caseId] ?? 'deep' },
        artifacts_latest: [],
        user_id: owner,
        owner_username: mockUsers.find((u) => u.id === owner)?.username ?? null,
        owner_display_name: mockUsers.find((u) => u.id === owner)?.display_name ?? null,
      }
      return jsonResponse(detail, 200)
    }

    // --- 会话快照 / 交付物 -----------------------------------------------
    if (method === 'GET' && messagesRe.test(path)) {
      return jsonResponse([] as Message[], 200)
    }
    if (method === 'GET' && artifactsRe.test(path)) {
      return jsonResponse([] as Artifact[], 200)
    }

    // --- 文件上传 ---------------------------------------------------------
    if (method === 'POST' && filesRe.test(path)) {
      const names = await readUploadNames(input, init)
      const files = names.map((name) => {
        mockFileSeq += 1
        return {
          id: `mock-file-${mockFileSeq}`,
          case_id: caseId,
          kind: 'upload',
          orig_name: name,
          mime: 'application/pdf',
          size: 1024 * 512,
          stored_path: `uploads/${caseId}/${name}`,
          md_path: null,
          meta: { pages: 14 },
          created_at: nowIso(),
        }
      })
      return jsonResponse(files, 201)
    }

    // --- 流水线 -----------------------------------------------------------
    if (method === 'GET' && stateRe.test(path)) {
      return jsonResponse(
        { run_group: 'initial', steps: [], pending_interaction: null },
        200,
      )
    }
    if (method === 'POST' && inputRe.test(path)) {
      resolveMockGate(caseId, 'input') // 放行时间线，续播下一段
      return jsonResponse({ ok: true }, 202)
    }
    if (method === 'POST' && commandRe.test(path)) {
      if (path.endsWith('/cancel')) resolveMockGate(caseId, 'force')
      return jsonResponse({ run_group: 'initial' }, 202)
    }

    // --- 交底书迭代（A-迭代：chat composer 发送） --------------------------
    if (method === 'POST' && iterateRe.test(path)) {
      resolveMockGate(caseId, 'iterate') // 放行交付后的迭代门控
      return jsonResponse({ run_group: 'iteration', intent: 'auto' }, 202)
    }

    // --- 专利解读产物（C2 权要树 / C6 report.json） -------------------------
    if (method === 'GET' && readerReportRe.test(path)) {
      return jsonResponse(
        {
          report: readerReport,
          markdown: readerReportMarkdown,
          artifact_id: readerDeliveryFile.artifact_id,
          version: 1,
          filename: readerDeliveryFile.filename,
          lint: [],
          unresolved: readerUnresolved,
        },
        200,
      )
    }
    if (method === 'GET' && readerTreeRe.test(path)) {
      return jsonResponse(
        {
          tree: { claims: readerClaims, ambiguities: readerAmbiguities },
          deltas: readerDeltas,
          summary: '共 10 项 · 独立 2 / 从属 8',
          lint: [],
          ambiguities_resolved: true,
        },
        200,
      )
    }

    // --- OA 案例库（§3.6 OACasesPage） --------------------------------------
    if (path.endsWith(`${API_BASE}/oa/library/search`) && method === 'GET') {
      const params = new URL(url, window.location.origin).searchParams
      const q = (params.get('q') ?? '').trim().toLowerCase()
      const k = Number(params.get('k') ?? 8) || 8
      const pool = oaLibrary.filter((row) => row.status === 'confirmed')
      const scored = pool
        .map((row, i) => {
          const haystack = [
            row.title,
            row.domain,
            (row.tags ?? []).join(' '),
            (row.statutes ?? []).join(' '),
            (row.defect_types ?? []).join(' '),
            oaLibraryNotes[row.id] ?? '',
          ]
            .join(' ')
            .toLowerCase()
          const hit = q ? haystack.includes(q) : true
          return {
            library_id: row.id,
            case_id: row.case_id,
            title: row.title,
            chunk_id: i + 1,
            chunk_index: i,
            chunk_text: (oaLibraryNotes[row.id] ?? row.title).slice(0, 160),
            score: hit ? 0.92 - i * 0.07 : 0.41 - i * 0.03,
            frontmatter: row.frontmatter,
            _hit: hit,
          }
        })
        .sort((a, b) => b.score - a.score)
      const hits = (scored.some((h) => h._hit) ? scored.filter((h) => h._hit) : scored).slice(0, k)
      return jsonResponse(
        {
          retrieval_mode: 'vector',
          query: params.get('q') ?? '',
          k,
          total_candidates: pool.length,
          hits: hits.map(({ _hit, ...rest }) => rest),
          filters: {},
          message: '',
          error: '',
        },
        200,
      )
    }
    if (path.endsWith(`${API_BASE}/oa/library/rebuild`) && method === 'POST') {
      return jsonResponse(
        {
          ok: true,
          queued: oaLibrary.filter((r) => r.status === 'confirmed').length,
          dim: 1024,
          mode: 'vector',
          vec_available: true,
          message: '',
        },
        202,
      )
    }
    if (path.endsWith(`${API_BASE}/oa/library/ingest`) && method === 'POST') {
      const names = await readUploadNames(input, init)
      mockLibrarySeq += 1
      const now = nowIso()
      const title = `${names[0] ?? '导入案例'} · 待人审草稿`
      const item = {
        id: `lib-new-${mockLibrarySeq}`,
        case_id: `OA-2026-90${mockLibrarySeq}`,
        title,
        status: 'draft',
        embedded: false,
        chunk_count: 0,
        patent_type: 'invention',
        statutes: ['专利法第22条第3款'],
        defect_types: ['inventiveness'],
        domain: '医学影像',
        outcome: 'unknown',
        tags: ['新导入'],
        frontmatter: {
          case_id: `OA-2026-90${mockLibrarySeq}`,
          title,
          status: 'draft',
          patent_type: 'invention',
          statutes: ['专利法第22条第3款'],
          defect_types: ['inventiveness'],
          outcome: 'unknown',
          redacted: true,
          tags: ['新导入'],
          source_paths: names,
          created_at: now,
          updated_at: now,
        },
        created_at: now,
        updated_at: now,
      }
      oaLibrary = [item, ...oaLibrary]
      oaLibraryNotes[item.id] = `## 案情摘要（草稿 · 待人审）\n\n由 ${names.join('、')} 生成的案例笔记草稿，请人工核对脱敏与结案信息后确认入库。`
      return jsonResponse(
        {
          item: { ...item, note_md: oaLibraryNotes[item.id], note_full_md: oaLibraryNotes[item.id] },
          redactions: [
            { original_hint: '申请人名称', replaced_with: '某医疗器械公司', reason: '主体脱敏' },
          ],
          inferred_fields: ['outcome'],
          normalized: [],
          materials: names,
        },
        201,
      )
    }
    if (oaLibraryIdRe.test(path)) {
      const libraryId = decodeURIComponent(oaLibraryIdRe.exec(path)?.[1] ?? '')
      const index = oaLibrary.findIndex((row) => row.id === libraryId)
      if (index < 0) return jsonResponse({ detail: '案例不存在' }, 404)
      if (method === 'DELETE') {
        oaLibrary = oaLibrary.filter((row) => row.id !== libraryId)
        return new Response(null, { status: 204 })
      }
      if (method === 'PUT') {
        const body = (await readJsonBody(input, init)) as {
          status?: string
          note_md?: string
        } | null
        const current = oaLibrary[index]
        const next = {
          ...current,
          status: body?.status ?? current.status,
          embedded: (body?.status ?? current.status) === 'confirmed',
          chunk_count:
            (body?.status ?? current.status) === 'confirmed'
              ? Math.max(current.chunk_count, 4)
              : current.chunk_count,
          updated_at: nowIso(),
          frontmatter: {
            ...current.frontmatter,
            status: body?.status ?? current.status,
            updated_at: nowIso(),
          },
        }
        if (body?.note_md) oaLibraryNotes[libraryId] = body.note_md
        oaLibrary = oaLibrary.map((row, i) => (i === index ? next : row))
        return jsonResponse(
          { ...next, note_md: oaLibraryNotes[libraryId] ?? '', note_full_md: oaLibraryNotes[libraryId] ?? '' },
          200,
        )
      }
      if (method === 'GET') {
        const row = oaLibrary[index]
        return jsonResponse(
          {
            ...row,
            note_md: oaLibraryNotes[libraryId] ?? '',
            note_full_md: oaLibraryNotes[libraryId] ?? '',
            redactions:
              row.status === 'draft'
                ? [
                    {
                      original_hint: '申请人名称',
                      replaced_with: '某医疗器械公司',
                      reason: '主体脱敏',
                    },
                  ]
                : [],
            inferred_fields: row.status === 'draft' ? ['outcome'] : [],
            chunks: [],
            embed_error: '',
          },
          200,
        )
      }
    }
    if (path.endsWith(`${API_BASE}/oa/library`) && method === 'GET') {
      const params = new URL(url, window.location.origin).searchParams
      const defectType = params.get('defect_type') ?? ''
      const status = params.get('status') ?? ''
      const keyword = (params.get('q') ?? '').trim().toLowerCase()
      const items = oaLibrary.filter((row) => {
        if (status && row.status !== status) return false
        if (defectType && !(row.defect_types ?? []).includes(defectType)) return false
        if (keyword && !`${row.title} ${(row.tags ?? []).join(' ')}`.toLowerCase().includes(keyword)) {
          return false
        }
        return true
      })
      return jsonResponse({ items, total: items.length, limit: 50, offset: 0 }, 200)
    }

    // --- p2p 专利内容 ------------------------------------------------------
    if (p2pContentRe.test(path)) {
      if (method === 'PUT') {
        const body = await readJsonBody(input, init)
        if (body && typeof body === 'object') p2pContent = body
        return jsonResponse(p2pContent, 200)
      }
      if (method === 'GET') return jsonResponse(p2pContent, 200)
    }

    /*
     * 安全网：mock 模式下 **任何** /api/v1 请求都不许穿透到 originalFetch。
     *
     * 真实后端很可能正跑在 8000 端口上，而 vite dev server 会把 /api 代理过去：
     * 一条漏网的 PUT /settings/llm 就能把开发者的真实 API Key 覆盖掉，
     * 一条漏网的 DELETE 就能删掉真案件。M8 之后还多一层——真实后端全局要求登录，
     * 漏网的 GET 会回 401，触发前端全局跳转，mock 模式直接被踢去登录页。
     * 因此这里一律就地返回 501，让缺失的 mock 端点在控制台里显形。
     */
    if (path.startsWith(API_BASE)) {
      console.warn(`[mock] 未实现的端点已被拦截，未穿透到真实后端：${method} ${path}`)
      return jsonResponse(
        { detail: `mock 模式未实现该端点：${method} ${path}` },
        501,
      )
    }
    return originalFetch(input, init)
  }

  restoreFetch = () => {
    window.fetch = originalFetch
    restoreFetch = null
  }
  return restoreFetch
}
