/**
 * SSE 客户端（frontend-design.md §5）：
 * - fetch + ReadableStream 手解（EventSource 无法带 header / 精细控制重连）；
 * - 缓冲 → 按空行切帧 → parse event:/data:/id:；
 * - AbortController 关闭；Last-Event-ID 续传（header + last_event_id query，
 *   对照 backend/app/api/events.py 的实际参数名）；
 * - 指数退避重连 1s→8s，最多 5 次；onEvent / onStateChange 回调；
 * - transport 可注入：VITE_USE_MOCKS=1 时默认换 mocks/mockServer 的 mock transport。
 */

import { API_BASE, notifyApiAuthFailure } from './api'

// ---------------------------------------------------------------------------
// 帧与传输层
// ---------------------------------------------------------------------------

/** 一帧原始 SSE 事件（data 为未解析字符串）。 */
export interface SseFrame {
  event: string
  data: string
  id?: string
}

export interface SseTransportOptions {
  url: string
  /** 续传游标（messages.seq）。 */
  lastEventId: string | null
  signal: AbortSignal
  /** 连接真正建立（HTTP 200 且开始读流）时回调 —— 客户端借此清零重试计数。 */
  onOpen: () => void
  onFrame: (frame: SseFrame) => void
}

/**
 * 传输层：打开一条流，正常结束时 resolve（视为服务端断开，触发重连），
 * 出错时 reject。abort 时应尽快退出（reject AbortError 或 resolve 均可）。
 */
export type SseTransport = (options: SseTransportOptions) => Promise<void>

// ---------------------------------------------------------------------------
// SSE 帧解析（按 WHATWG SSE 规范的字段语义）
// ---------------------------------------------------------------------------

export interface SseFrameParser {
  /** 喂入一段解码后的文本。 */
  push: (chunk: string) => void
  /** 流结束时冲洗残留缓冲。 */
  flush: () => void
}

/** 增量帧解析器：缓冲 → 空行切帧 → 逐行解析字段。 */
export function createSseFrameParser(onFrame: (frame: SseFrame) => void): SseFrameParser {
  let buffer = ''

  const parseFrame = (raw: string): void => {
    let event = 'message'
    let id: string | undefined
    const dataLines: string[] = []

    for (const line of raw.split('\n')) {
      if (line === '' || line.startsWith(':')) continue // 注释 / 空行
      const colon = line.indexOf(':')
      const field = colon === -1 ? line : line.slice(0, colon)
      let value = colon === -1 ? '' : line.slice(colon + 1)
      if (value.startsWith(' ')) value = value.slice(1)

      if (field === 'event') event = value || 'message'
      else if (field === 'data') dataLines.push(value)
      else if (field === 'id' && !value.includes('\0')) id = value
      // retry 字段由客户端退避策略接管，忽略
    }

    if (dataLines.length === 0 && event === 'message') return // 纯 id/注释帧
    onFrame({ event, data: dataLines.join('\n'), id })
  }

  const drain = (final: boolean): void => {
    // 统一行尾（\r\n / \r → \n）后按空行切帧
    buffer = buffer.replace(/\r\n/g, '\n').replace(/\r/g, '\n')
    let sep = buffer.indexOf('\n\n')
    while (sep !== -1) {
      const raw = buffer.slice(0, sep)
      buffer = buffer.slice(sep + 2)
      if (raw.trim() !== '') parseFrame(raw)
      sep = buffer.indexOf('\n\n')
    }
    if (final && buffer.trim() !== '') {
      parseFrame(buffer)
      buffer = ''
    }
  }

  return {
    push: (chunk) => {
      buffer += chunk
      drain(false)
    },
    flush: () => drain(true),
  }
}

// ---------------------------------------------------------------------------
// 默认 fetch 传输层
// ---------------------------------------------------------------------------

function withCursor(url: string, lastEventId: string | null): string {
  if (!lastEventId) return url
  const sep = url.includes('?') ? '&' : '?'
  return `${url}${sep}last_event_id=${encodeURIComponent(lastEventId)}`
}

/** 生产传输层：fetch + ReadableStream 手动读流解帧。 */
export const fetchSseTransport: SseTransport = async ({
  url,
  lastEventId,
  signal,
  onOpen,
  onFrame,
}) => {
  const headers: Record<string, string> = { Accept: 'text/event-stream' }
  if (lastEventId) headers['Last-Event-ID'] = lastEventId

  const res = await fetch(withCursor(url, lastEventId), {
    headers,
    signal,
    cache: 'no-store',
  })
  if (!res.ok) {
    /*
     * 会话过期时事件流也会 401。不在这里派发全局处理的话，工作台只会陷入
     * 「连接中断，正在第 N 次重连」的死循环——用户看不出自己其实已经掉线了。
     */
    notifyApiAuthFailure(res.status)
    throw new Error(`SSE 连接失败（HTTP ${res.status}）`)
  }
  if (!res.body) throw new Error('SSE 响应无正文流')

  onOpen()

  const parser = createSseFrameParser(onFrame)
  const reader = res.body.getReader()
  const decoder = new TextDecoder('utf-8')
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      parser.push(decoder.decode(value, { stream: true }))
    }
    parser.push(decoder.decode())
    parser.flush()
  } finally {
    reader.releaseLock()
  }
}

// ---------------------------------------------------------------------------
// 默认传输层解析（VITE_USE_MOCKS=1 → mock transport）
// ---------------------------------------------------------------------------

let defaultTransportPromise: Promise<SseTransport> | null = null

/**
 * 是否处于 mock 模式（VITE_USE_MOCKS=1）。
 *
 * 注意：所有 `import('../mocks/mockServer')` 的调用点都必须**内联**写
 * `import.meta.env.VITE_USE_MOCKS === '1'`，不能改调本函数——Vite 只对内联的
 * `import.meta.env.X` 做常量替换，跨模块的函数调用无法被折叠，
 * 于是 97KB 的 mockServer chunk 会永远留在生产 dist 里（虽然运行时永不请求）。
 */
export function isMockMode(): boolean {
  return import.meta.env.VITE_USE_MOCKS === '1'
}

/** 解析默认传输层：mock 模式动态引入 mockServer，否则用 fetch 传输层。 */
export function resolveDefaultTransport(): Promise<SseTransport> {
  if (!defaultTransportPromise) {
    defaultTransportPromise =
      import.meta.env.VITE_USE_MOCKS === '1'
        ? import('../mocks/mockServer').then((m) => m.createMockSseTransport())
        : Promise.resolve(fetchSseTransport)
  }
  return defaultTransportPromise
}

// ---------------------------------------------------------------------------
// 客户端（退避重连 + 状态机）
// ---------------------------------------------------------------------------

export type SseConnectionState =
  | 'idle'
  | 'connecting'
  | 'open'
  | 'reconnecting'
  | 'failed'
  | 'closed'

export interface SseStateChangeInfo {
  /** 当前第几次重试（0 = 首连）。 */
  attempt: number
  error?: unknown
}

export interface SseClientOptions {
  /** 完整事件流 URL（一般由 caseEventsUrl 构造）。 */
  url: string
  onEvent: (frame: SseFrame) => void
  onStateChange?: (state: SseConnectionState, info: SseStateChangeInfo) => void
  /** 注入传输层；缺省按 VITE_USE_MOCKS 解析。 */
  transport?: SseTransport
  /** 最多重试次数（默认 5）。 */
  maxRetries?: number
  /** 退避基数 ms（默认 1000；序列 1s→2s→4s→8s→8s）。 */
  baseDelayMs?: number
  /** 退避上限 ms（默认 8000）。 */
  maxDelayMs?: number
  /** 初始续传游标（如从快照接续）。 */
  initialLastEventId?: string | null
}

export interface SseClient {
  start: () => void
  close: () => void
  getState: () => SseConnectionState
  getLastEventId: () => string | null
}

/** 案件事件流 URL。 */
export function caseEventsUrl(caseId: string): string {
  return `${API_BASE}/cases/${encodeURIComponent(caseId)}/events`
}

export function createSseClient(options: SseClientOptions): SseClient {
  const {
    url,
    onEvent,
    onStateChange,
    transport,
    maxRetries = 5,
    baseDelayMs = 1000,
    maxDelayMs = 8000,
    initialLastEventId = null,
  } = options

  let state: SseConnectionState = 'idle'
  let lastEventId: string | null = initialLastEventId
  let closed = false
  let started = false
  let controller: AbortController | null = null
  let backoffTimer: ReturnType<typeof setTimeout> | null = null
  let wakeBackoff: (() => void) | null = null

  const setState = (next: SseConnectionState, info: SseStateChangeInfo): void => {
    state = next
    onStateChange?.(next, info)
  }

  /** 可被 close() 提前唤醒的退避等待。 */
  const backoffSleep = (ms: number): Promise<void> =>
    new Promise((resolve) => {
      wakeBackoff = () => {
        wakeBackoff = null
        resolve()
      }
      backoffTimer = setTimeout(() => {
        backoffTimer = null
        wakeBackoff = null
        resolve()
      }, ms)
    })

  const run = async (): Promise<void> => {
    const activeTransport = transport ?? (await resolveDefaultTransport())
    let attempt = 0
    let everOpened = false

    while (!closed) {
      setState(everOpened || attempt > 0 ? 'reconnecting' : 'connecting', { attempt })
      controller = new AbortController()
      let openedThisRound = false
      try {
        await activeTransport({
          url,
          lastEventId,
          signal: controller.signal,
          onOpen: () => {
            openedThisRound = true
            everOpened = true
            attempt = 0
            setState('open', { attempt: 0 })
          },
          onFrame: (frame) => {
            if (frame.id) lastEventId = frame.id
            onEvent(frame)
          },
        })
        // 传输层正常返回 = 服务端断开 → 走重连
        if (closed) break
        throw new Error('SSE 连接已断开')
      } catch (error) {
        if (closed) break
        // 未成功打开且立即失败，也计一次重试
        void openedThisRound
        attempt += 1
        if (attempt > maxRetries) {
          setState('failed', { attempt, error })
          return
        }
        setState('reconnecting', { attempt, error })
        const delay = Math.min(baseDelayMs * 2 ** (attempt - 1), maxDelayMs)
        await backoffSleep(delay)
      }
    }
    setState('closed', { attempt: 0 })
  }

  return {
    start: () => {
      if (started) return
      started = true
      void run()
    },
    close: () => {
      if (closed) return
      closed = true
      controller?.abort()
      if (backoffTimer !== null) {
        clearTimeout(backoffTimer)
        backoffTimer = null
      }
      wakeBackoff?.()
    },
    getState: () => state,
    getLastEventId: () => lastEventId,
  }
}
