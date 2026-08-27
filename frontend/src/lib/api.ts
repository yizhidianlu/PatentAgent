/**
 * fetch 封装：一切请求挂 /api/v1（backend-architecture.md §3），
 * JSON 错误规整为 ApiError。
 *
 * M8 认证接入（auth-system.md §3 / §6）：
 * - 会话走 httpOnly Cookie（`pa_session`），前端读不到也不需要读；
 * - CSRF 走双提交：后端把 token 写进非 httpOnly 的 `pa_csrf` Cookie，
 *   本层给**所有非 GET/HEAD 请求**自动补 `X-CSRF-Token` 头；
 * - 401（未登录 / 会话过期）→ 清本地状态并跳 /login；
 * - 403 且 body 含 `{"code":"must_change_password"}` → 跳 /change-password；
 * - 429（配额超限）→ 弹 toast 提示联系管理员。
 */

import { zh } from '../i18n/zh'
import { useUiStore } from '../stores/uiStore'

export const API_BASE = '/api/v1'

/** 后端下发的 CSRF Cookie 名（非 httpOnly，供本层读取回填请求头）。 */
export const CSRF_COOKIE = 'pa_csrf'
/** 双提交校验的请求头名（与 backend/app/services/auth.py CSRF_HEADER 一致）。 */
export const CSRF_HEADER = 'X-CSRF-Token'

/** 幂等方法免 CSRF（与后端中间件 SAFE_METHODS 对齐）。 */
const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS'])

export class ApiError extends Error {
  readonly status: number
  readonly detail: unknown

  constructor(status: number, message: string, detail?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

export interface RequestOptions extends Omit<RequestInit, 'body'> {
  /** 普通对象自动 JSON 序列化；FormData / Blob / 字符串原样透传。 */
  body?: unknown
  /** query 参数；undefined / null / '' 的键自动剔除。 */
  query?: Record<string, string | number | boolean | undefined | null>
  /**
   * 跳过 401 / 403-must_change_password 的全局跳转。
   * 登录探测（GET /auth/me）与登录本身必须置位：它们的 401 是**预期结果**，
   * 不置位会在登录页里自己把自己再踢一次，用户看到页面无故刷新。
   */
  skipAuthHandling?: boolean
}

// ---------------------------------------------------------------------------
// CSRF
// ---------------------------------------------------------------------------

/** 读 `pa_csrf` Cookie；不存在（未登录 / 已过期）返回空串。 */
export function readCsrfToken(): string {
  if (typeof document === 'undefined') return ''
  for (const part of document.cookie.split(';')) {
    const raw = part.trim()
    if (!raw.startsWith(`${CSRF_COOKIE}=`)) continue
    const value = raw.slice(CSRF_COOKIE.length + 1)
    try {
      return decodeURIComponent(value)
    } catch {
      return value
    }
  }
  return ''
}

// ---------------------------------------------------------------------------
// 全局认证失败处理
// ---------------------------------------------------------------------------

export type ApiAuthFailure = 'unauthorized' | 'must_change_password'

type AuthFailureHandler = (kind: ApiAuthFailure, error: ApiError) => void

let authFailureHandler: AuthFailureHandler | null = null

/**
 * 注册全局认证失败处理（由 routes 层的 AuthBridge 挂上，用 react-router 跳转，
 * 避免整页刷新丢掉未提交的表单）。返回注销函数。
 *
 * 之所以用注册而非直接 import router：router.tsx → pages → api → lib/api，
 * 反向 import 会成环。
 */
export function setApiAuthHandler(handler: AuthFailureHandler | null): () => void {
  authFailureHandler = handler
  return () => {
    if (authFailureHandler === handler) authFailureHandler = null
  }
}

/** body 是否为「必须先改密」的 403（后端 middleware.py 下发 code 字段）。 */
export function isMustChangePassword(status: number, payload: unknown): boolean {
  if (status !== 403 || !payload || typeof payload !== 'object') return false
  return (payload as { code?: unknown }).code === 'must_change_password'
}

function fallbackRedirect(path: string): void {
  if (typeof window === 'undefined') return
  if (window.location.pathname === path) return
  window.location.assign(path)
}

function dispatchAuthFailure(kind: ApiAuthFailure, error: ApiError): void {
  if (authFailureHandler) {
    authFailureHandler(kind, error)
    return
  }
  fallbackRedirect(kind === 'unauthorized' ? '/login' : '/change-password')
}

/**
 * 供绕开 `request()` 的传输层复用（XHR 上传、SSE 连接）：
 * 按状态码派发全局认证失败处理，让它们的 401 也能正常跳登录页。
 */
export function notifyApiAuthFailure(status: number, payload?: unknown): void {
  if (status === 401) {
    dispatchAuthFailure('unauthorized', new ApiError(status, zh.auth.sessionExpired, payload))
    return
  }
  if (isMustChangePassword(status, payload)) {
    dispatchAuthFailure(
      'must_change_password',
      new ApiError(status, zh.auth.changePassword.forcedTitle, payload),
    )
  }
}

/** 429 配额提示：同一窗口内 5s 只弹一次，避免并发请求刷屏。 */
let lastQuotaToastAt = 0
function toastQuotaExceeded(message: string): void {
  const now = Date.now()
  if (now - lastQuotaToastAt < 5_000) return
  lastQuotaToastAt = now
  useUiStore.getState().pushToast('warning', message || zh.auth.quotaExceeded)
}

function buildUrl(path: string, query?: RequestOptions['query']): string {
  const url = path.startsWith('/') ? `${API_BASE}${path}` : `${API_BASE}/${path}`
  if (!query) return url
  const params = new URLSearchParams()
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null || v === '') continue
    params.set(k, String(v))
  }
  const qs = params.toString()
  return qs ? `${url}?${qs}` : url
}

/** 从 JSON / 文本错误体尽力提取人类可读错误消息（FastAPI detail 兼容）。 */
function extractErrorMessage(status: number, payload: unknown): string {
  if (payload && typeof payload === 'object') {
    const obj = payload as Record<string, unknown>
    const detail = obj.detail ?? obj.message ?? obj.error
    if (typeof detail === 'string' && detail) return detail
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0] as Record<string, unknown> | string
      if (typeof first === 'string') return first
      if (first && typeof first.msg === 'string') return first.msg
    }
  }
  if (typeof payload === 'string' && payload) return payload
  return zh.common.httpFailed(status)
}

/** 发起请求并返回解析后的 JSON；非 2xx 抛 ApiError。204 返回 undefined。 */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { body, query, headers, skipAuthHandling = false, ...rest } = options
  const init: RequestInit = { credentials: 'same-origin', ...rest }

  const finalHeaders = new Headers(headers)
  if (body !== undefined) {
    if (body instanceof FormData || body instanceof Blob || typeof body === 'string') {
      init.body = body as BodyInit
    } else {
      init.body = JSON.stringify(body)
      if (!finalHeaders.has('Content-Type')) {
        finalHeaders.set('Content-Type', 'application/json')
      }
    }
  }

  // CSRF 双提交：非幂等方法一律补 X-CSRF-Token（缺 token 时也照发，
  // 由后端回 403 并由上层提示刷新，比前端擅自静默放行安全）
  const method = (init.method ?? 'GET').toUpperCase()
  if (!SAFE_METHODS.has(method) && !finalHeaders.has(CSRF_HEADER)) {
    const token = readCsrfToken()
    if (token) finalHeaders.set(CSRF_HEADER, token)
  }
  init.headers = finalHeaders

  let res: Response
  try {
    res = await fetch(buildUrl(path, query), init)
  } catch (e) {
    throw new ApiError(0, zh.common.networkDown, e)
  }

  if (res.status === 204) return undefined as T

  const contentType = res.headers.get('content-type') ?? ''
  let payload: unknown = null
  try {
    payload = contentType.includes('application/json') ? await res.json() : await res.text()
  } catch {
    payload = null
  }

  if (!res.ok) {
    const error = new ApiError(res.status, extractErrorMessage(res.status, payload), payload)
    if (res.status === 429) toastQuotaExceeded(error.message)
    if (!skipAuthHandling) {
      if (res.status === 401) dispatchAuthFailure('unauthorized', error)
      else if (isMustChangePassword(res.status, payload)) {
        dispatchAuthFailure('must_change_password', error)
      }
    }
    throw error
  }
  return payload as T
}

export const api = {
  get: <T>(path: string, options?: RequestOptions) =>
    request<T>(path, { ...options, method: 'GET' }),
  post: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    request<T>(path, { ...options, method: 'POST', body }),
  put: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    request<T>(path, { ...options, method: 'PUT', body }),
  patch: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    request<T>(path, { ...options, method: 'PATCH', body }),
  delete: <T>(path: string, options?: RequestOptions) =>
    request<T>(path, { ...options, method: 'DELETE' }),
}
