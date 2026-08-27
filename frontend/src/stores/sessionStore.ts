/**
 * 会话流状态（frontend-design.md §5）：
 * Record<caseId, { items, docs, pipeline, connection }> + SSE 事件 reducer。
 * - llm_delta chat 通道 → 追加到当前 streaming assistant 条目；
 * - llm_delta doc 通道 → 追加到 docs[docId].markdown（并保证流内 doc_ref 条目存在）；
 * - interaction_required → 推 stage 条目（仅最新 stage 保持 active）；
 * - delta 经 rAF 缓冲（隔帧刷新 ≈30fps）批量刷入 zustand，其余事件先冲洗缓冲再即时应用。
 */

import { create } from 'zustand'
import type { SseFrame, SseConnectionState } from '../lib/sse'
import type { Artifact, FileMeta, Message, PipelineState, StepRunStatus } from '../types/models'
import type {
  AssistantStreamItem,
  CaseSseEventMap,
  DocDownloadEntry,
  DocVersionEvent,
  NoticeStreamItem,
  Stage,
  StageStatus,
  StreamItem,
  StepProgressEvent,
} from '../types/stream'
import { isCaseSseEventName } from '../types/stream'

// ---------------------------------------------------------------------------
// 状态形状
// ---------------------------------------------------------------------------

/** 缺省文档 id（llm_delta doc 通道未带 doc_id 时归入）。 */
export const DEFAULT_DOC_ID = 'main'

export interface DocState {
  markdown: string
  streaming: boolean
  versionId?: string
  version?: number
  filename?: string
  /** 最新版本各格式下载项（doc_version.downloads）。 */
  downloads?: DocDownloadEntry[]
  /** 最新版本主交付物 id。 */
  artifactId?: string
  /**
   * 正文来自「已有交付物回填」而非本次实时流式（刷新恢复）。
   * 新一轮 doc 通道的首个 delta 到达时据此清空——否则历史正文会和
   * 新流式内容首尾相接，拼出一份两倍长的假文档。
   */
  hydrated?: boolean
}

export interface PipelineStepState {
  key: string
  nameZh: string
  status: StepRunStatus
  attempt?: number
  error?: string | null
}

export interface CaseSessionState {
  items: StreamItem[]
  docs: Record<string, DocState>
  /** 文档出现顺序（activeDocId 之外的多文档场景保序）。 */
  docOrder: string[]
  /** 当前展示在右侧面板的文档。 */
  activeDocId: string | null
  pipeline: {
    steps: PipelineStepState[]
    currentKey: string | null
    done: boolean
  }
  connection: SseConnectionState
  connectionAttempt: number
  title: string | null
  /** 版本历史（doc_version 事件累积，VersionHistory 数据源）。 */
  versions: DocVersionEvent[]
  artifacts: Artifact[]
  lastError: { message: string; retryable: boolean } | null
  /**
   * 当前步骤的实时进度（step_progress 心跳）。
   *
   * 步骤切换 / 流水线结束时清空 —— 留着上一步的进度比没有进度更误导：
   * 用户会以为系统还停在那一步。
   */
  progress: StepProgressEvent | null
  /** 最近一次收到心跳的本地时刻（毫秒）。用来判断「事件流本身是不是断了」。 */
  progressAt: number | null
}

export function emptySession(): CaseSessionState {
  return {
    items: [],
    docs: {},
    docOrder: [],
    activeDocId: null,
    pipeline: { steps: [], currentKey: null, done: false },
    connection: 'idle',
    connectionAttempt: 0,
    title: null,
    versions: [],
    artifacts: [],
    lastError: null,
    progress: null,
    progressAt: null,
  }
}

// ---------------------------------------------------------------------------
// store 定义
// ---------------------------------------------------------------------------

export interface SessionStoreState {
  sessions: Record<string, CaseSessionState>

  ensureSession: (caseId: string) => void
  resetSession: (caseId: string) => void
  setConnection: (caseId: string, state: SseConnectionState, attempt?: number) => void
  /** SSE 帧入口：解析 data JSON 并按事件表归约。 */
  handleSseFrame: (caseId: string, frame: SseFrame) => void
  pushUserMessage: (caseId: string, text: string, files?: FileMeta[]) => void
  /** HITL 卡片提交后乐观置 completed（§4）。 */
  completeStage: (caseId: string, stageId: string, result?: unknown) => void
  skipStage: (caseId: string, stageId: string) => void
  /**
   * 用 GET /cases/{id}/messages 快照种流（仅在流为空时执行，避免与 SSE 重放重复）。
   * 返回快照最大 seq（作为 SSE Last-Event-ID 续传游标）；未种入时返回 null。
   */
  seedMessages: (caseId: string, messages: Message[]) => string | null
  /** 用 GET /cases/{id}/pipeline/state 水合步骤条与待确认交互（刷新恢复）。 */
  hydratePipeline: (caseId: string, state: PipelineState) => void
  /**
   * 用已有交付物正文回填文档面板（刷新恢复）。
   * 实时流式绝对优先：目标文档正在流式或已有正文时直接放弃回填。
   */
  hydrateDocContent: (caseId: string, docId: string, patch: DocContentPatch) => void
}

/** hydrateDocContent 的载荷（源自 GET /artifacts/{id}/content）。 */
export interface DocContentPatch {
  markdown: string
  versionId?: string
  version?: number
  filename?: string
  artifactId?: string
}

let idSeq = 0
function nextId(prefix: string): string {
  idSeq += 1
  return `${prefix}-${idSeq}`
}

function safeParseJson(text: string): unknown {
  try {
    return JSON.parse(text)
  } catch {
    return null
  }
}

// ---------------------------------------------------------------------------
// rAF delta 缓冲（模块级，避免每 delta 一次 set()）
// ---------------------------------------------------------------------------

interface PendingDeltas {
  /** assistant 条目 id → 待追加文本（保持插入顺序）。 */
  chat: Map<string, string>
  /** doc id → 待追加文本。 */
  doc: Map<string, string>
}

const pendingByCase = new Map<string, PendingDeltas>()
let flushScheduled = false

function getPending(caseId: string): PendingDeltas {
  let p = pendingByCase.get(caseId)
  if (!p) {
    p = { chat: new Map(), doc: new Map() }
    pendingByCase.set(caseId, p)
  }
  return p
}

const raf: (cb: () => void) => void =
  typeof requestAnimationFrame === 'function'
    ? (cb) => requestAnimationFrame(() => cb())
    : (cb) => void setTimeout(cb, 16)

/** 隔一帧再刷（双重 rAF ≈ 30fps），削减高频 delta 下的渲染压力。 */
function scheduleFlush(): void {
  if (flushScheduled) return
  flushScheduled = true
  raf(() =>
    raf(() => {
      flushScheduled = false
      flushDeltas()
    }),
  )
}

function flushDeltas(): void {
  if (pendingByCase.size === 0) return
  const entries = [...pendingByCase.entries()]
  pendingByCase.clear()

  useSessionStore.setState((state) => {
    const sessions = { ...state.sessions }
    for (const [caseId, pending] of entries) {
      const session = sessions[caseId]
      if (!session) continue
      let next = session

      if (pending.chat.size > 0) {
        const items = next.items.map((item) => {
          if (item.kind !== 'assistant') return item
          const addition = pending.chat.get(item.id)
          if (!addition) return item
          return { ...item, markdown: item.markdown + addition }
        })
        next = { ...next, items }
      }
      if (pending.doc.size > 0) {
        const docs = { ...next.docs }
        for (const [docId, addition] of pending.doc) {
          const doc = docs[docId] ?? { markdown: '', streaming: true }
          docs[docId] = { ...doc, markdown: doc.markdown + addition, streaming: true }
        }
        next = { ...next, docs }
      }
      sessions[caseId] = next
    }
    return { sessions }
  })
}

/** 立即冲洗缓冲（非 delta 事件到达前调用，保证事件次序）。 */
function flushNow(): void {
  flushDeltas()
}

// ---------------------------------------------------------------------------
// 会话内不可变更新工具
// ---------------------------------------------------------------------------

type SessionUpdater = (session: CaseSessionState) => CaseSessionState

function updateSession(caseId: string, updater: SessionUpdater): void {
  useSessionStore.setState((state) => {
    const session = state.sessions[caseId] ?? emptySession()
    return { sessions: { ...state.sessions, [caseId]: updater(session) } }
  })
}

function setStageStatus(
  session: CaseSessionState,
  stageId: string,
  status: StageStatus,
  result?: unknown,
): CaseSessionState {
  const items = session.items.map((item) => {
    if (item.kind !== 'stage' || item.stage.id !== stageId) return item
    const stage: Stage = { ...item.stage, status, ...(result !== undefined ? { result } : {}) }
    return { ...item, stage }
  })
  return { ...session, items }
}

/** 找到当前应接收 chat delta 的 assistant 条目；没有则创建。返回条目 id。 */
function ensureStreamingAssistant(
  session: CaseSessionState,
  stepKey: string | null,
): { session: CaseSessionState; itemId: string } {
  const last = session.items[session.items.length - 1]
  if (last && last.kind === 'assistant' && last.streaming) {
    return { session, itemId: last.id }
  }
  const item: AssistantStreamItem = {
    kind: 'assistant',
    id: nextId('asst'),
    markdown: '',
    streaming: true,
    stepKey,
  }
  return { session: { ...session, items: [...session.items, item] }, itemId: item.id }
}

/** 保证 doc 状态与流内 doc_ref 条目存在。 */
function ensureDoc(session: CaseSessionState, docId: string): CaseSessionState {
  let next = session
  if (!next.docs[docId]) {
    next = {
      ...next,
      docs: { ...next.docs, [docId]: { markdown: '', streaming: false } },
      docOrder: [...next.docOrder, docId],
    }
  }
  if (!next.items.some((item) => item.kind === 'doc_ref' && item.docId === docId)) {
    next = {
      ...next,
      items: [...next.items, { kind: 'doc_ref', id: nextId('docref'), docId }],
    }
  }
  if (next.activeDocId !== docId) next = { ...next, activeDocId: docId }
  return next
}

function upsertStep(
  session: CaseSessionState,
  data: CaseSseEventMap['step_status'],
): CaseSessionState {
  const steps = [...session.pipeline.steps]
  const idx = steps.findIndex((s) => s.key === data.step_key)
  const patch: PipelineStepState = {
    key: data.step_key,
    nameZh: data.name_zh ?? (idx >= 0 ? steps[idx].nameZh : data.step_key),
    status: data.status,
    attempt: data.attempt,
    error: data.error ?? null,
  }
  if (idx >= 0) steps[idx] = { ...steps[idx], ...patch }
  else steps.push(patch)

  const currentKey =
    data.status === 'running' || data.status === 'waiting_user'
      ? data.step_key
      : session.pipeline.currentKey
  return { ...session, pipeline: { ...session.pipeline, steps, currentKey } }
}

type NoticeExtras = Pick<NoticeStreamItem, 'level' | 'retryable' | 'stepKey' | 'stepLabel'>

function pushNotice(
  session: CaseSessionState,
  text: string,
  tag?: string,
  extras?: NoticeExtras,
): CaseSessionState {
  // 同 tag 的连续 notice 原地更新（如 search_progress 滚动进度）
  const last = session.items[session.items.length - 1]
  if (tag && last && last.kind === 'notice' && last.tag === tag) {
    const items = session.items.slice(0, -1)
    items.push({ ...last, text, ...extras })
    return { ...session, items }
  }
  return {
    ...session,
    items: [...session.items, { kind: 'notice', id: nextId('notice'), text, tag, ...extras }],
  }
}

// ---------------------------------------------------------------------------
// SSE 事件 reducer
// ---------------------------------------------------------------------------

function reduceEvent(caseId: string, event: string, data: unknown): void {
  switch (event) {
    case 'llm_delta': {
      const d = data as CaseSseEventMap['llm_delta']
      if (!d || typeof d.text !== 'string') return
      if (d.channel === 'doc') {
        const docId = d.doc_id ?? DEFAULT_DOC_ID
        updateSession(caseId, (session) => {
          let next = ensureDoc(session, docId)
          const doc = next.docs[docId]
          if (!doc.streaming || doc.hydrated) {
            next = {
              ...next,
              docs: {
                ...next.docs,
                [docId]: {
                  ...doc,
                  // 回填的历史正文让位给实时流式：首个 delta 到达即清空
                  markdown: doc.hydrated ? '' : doc.markdown,
                  hydrated: false,
                  streaming: true,
                },
              },
            }
          }
          return next
        })
        const pending = getPending(caseId)
        pending.doc.set(docId, (pending.doc.get(docId) ?? '') + d.text)
      } else {
        let targetId = ''
        updateSession(caseId, (session) => {
          const ensured = ensureStreamingAssistant(session, d.step_key ?? null)
          targetId = ensured.itemId
          return ensured.session
        })
        const pending = getPending(caseId)
        pending.chat.set(targetId, (pending.chat.get(targetId) ?? '') + d.text)
      }
      scheduleFlush()
      return
    }

    case 'llm_done': {
      flushNow()
      const d = data as CaseSseEventMap['llm_done']
      updateSession(caseId, (session) => {
        if (d?.channel === 'doc') {
          const docId = d.doc_id ?? DEFAULT_DOC_ID
          const doc = session.docs[docId]
          if (!doc) return session
          return { ...session, docs: { ...session.docs, [docId]: { ...doc, streaming: false } } }
        }
        // chat：结束最后一个 streaming assistant 条目
        const items = [...session.items]
        for (let i = items.length - 1; i >= 0; i -= 1) {
          const item = items[i]
          if (item.kind === 'assistant' && item.streaming) {
            items[i] = { ...item, streaming: false }
            break
          }
        }
        return { ...session, items }
      })
      return
    }

    case 'step_progress': {
      const d = data as CaseSseEventMap['step_progress']
      if (!d?.step_key) return
      // 不 flushNow()：心跳每 5 秒一拍，为它打断 delta 缓冲会让流式渲染变卡
      updateSession(caseId, (session) => ({
        ...session,
        progress: d,
        progressAt: Date.now(),
      }))
      return
    }

    case 'step_status': {
      flushNow()
      const d = data as CaseSseEventMap['step_status']
      if (!d?.step_key) return
      updateSession(caseId, (session) => {
        const next = upsertStep(session, d)
        // 换步骤或本步已结束 → 丢掉旧进度。留着它会让用户以为还停在上一步
        const stale =
          d.status !== 'running' || session.progress?.step_key !== d.step_key
        return stale ? { ...next, progress: null, progressAt: null } : next
      })
      return
    }

    case 'doc_version': {
      flushNow()
      const d = data as CaseSseEventMap['doc_version']
      if (!d) return
      const docId = d.doc_id ?? DEFAULT_DOC_ID
      updateSession(caseId, (session) => {
        let next = ensureDoc(session, docId)
        const doc = next.docs[docId]
        next = {
          ...next,
          docs: {
            ...next.docs,
            [docId]: {
              ...doc,
              streaming: false,
              versionId: d.version_id,
              version: d.version,
              filename: d.filename,
              downloads: d.downloads,
              artifactId: d.artifact_id,
            },
          },
          versions: [...next.versions, d],
        }
        return next
      })
      return
    }

    case 'interaction_required': {
      flushNow()
      const d = data as CaseSseEventMap['interaction_required']
      if (!d?.kind) return
      updateSession(caseId, (session) => {
        // 仅最新 stage 可 active（§4）：旧 active 卡折叠为 completed
        let next = session
        for (const item of session.items) {
          if (item.kind === 'stage' && item.stage.status === 'active') {
            next = setStageStatus(next, item.stage.id, 'completed')
          }
        }
        const stage: Stage = {
          id: nextId('stage'),
          type: d.kind,
          status: 'active',
          payload: d,
          stepKey: d.step_key,
        }
        return {
          ...next,
          items: [...next.items, { kind: 'stage', id: stage.id, stage }],
        }
      })
      return
    }

    case 'search_progress': {
      flushNow()
      const d = data as CaseSseEventMap['search_progress']
      if (!d?.message) return
      updateSession(caseId, (session) => pushNotice(session, d.message, 'search_progress'))
      return
    }

    case 'artifact_created': {
      flushNow()
      const d = data as CaseSseEventMap['artifact_created']
      if (!d?.id) return
      updateSession(caseId, (session) => ({ ...session, artifacts: [...session.artifacts, d] }))
      return
    }

    case 'case_title': {
      flushNow()
      const d = data as CaseSseEventMap['case_title']
      if (!d?.title) return
      updateSession(caseId, (session) => ({ ...session, title: d.title }))
      return
    }

    case 'log': {
      flushNow()
      const d = data as CaseSseEventMap['log']
      if (!d?.message) return
      // level=error 的日志也走失败卡（不可重试），避免混在灰色 notice 里被忽略
      const extras = d.level === 'error' ? ({ level: 'error' as const, retryable: false }) : undefined
      updateSession(caseId, (session) => pushNotice(session, d.message, undefined, extras))
      return
    }

    case 'error': {
      flushNow()
      const d = data as CaseSseEventMap['error']
      if (!d?.message) return
      updateSession(caseId, (session) => {
        // 失败步骤中文名（失败卡标题）取自已收到的 step_status
        const stepKey = d.step_key ?? session.pipeline.currentKey ?? undefined
        const stepLabel = stepKey
          ? session.pipeline.steps.find((s) => s.key === stepKey)?.nameZh
          : undefined
        return {
          ...pushNotice(session, d.message, undefined, {
            level: 'error',
            retryable: Boolean(d.retryable),
            stepKey,
            stepLabel,
          }),
          lastError: { message: d.message, retryable: Boolean(d.retryable) },
        }
      })
      return
    }

    case 'pipeline_done': {
      flushNow()
      updateSession(caseId, (session) => {
        const docs: Record<string, DocState> = {}
        for (const [docId, doc] of Object.entries(session.docs)) {
          docs[docId] = doc.streaming ? { ...doc, streaming: false } : doc
        }
        return {
          ...session,
          docs,
          pipeline: { ...session.pipeline, currentKey: null, done: true },
          progress: null,
          progressAt: null,
        }
      })
      return
    }

    case 'ping':
      return

    default:
      // 未知事件（如后端扩展）：仅在 dev 提示，不入流
      if (import.meta.env.DEV && !isCaseSseEventName(event)) {
        console.debug(`[sessionStore] 未处理的 SSE 事件：${event}`, data)
      }
  }
}

// ---------------------------------------------------------------------------
// store 实例
// ---------------------------------------------------------------------------

export const useSessionStore = create<SessionStoreState>((set, get) => ({
  sessions: {},

  ensureSession: (caseId) => {
    if (get().sessions[caseId]) return
    set((state) => ({ sessions: { ...state.sessions, [caseId]: emptySession() } }))
  },

  resetSession: (caseId) => {
    pendingByCase.delete(caseId)
    set((state) => ({ sessions: { ...state.sessions, [caseId]: emptySession() } }))
  },

  setConnection: (caseId, connection, attempt = 0) => {
    updateSession(caseId, (session) => ({ ...session, connection, connectionAttempt: attempt }))
  },

  handleSseFrame: (caseId, frame) => {
    get().ensureSession(caseId)
    reduceEvent(caseId, frame.event, safeParseJson(frame.data))
  },

  pushUserMessage: (caseId, text, files) => {
    get().ensureSession(caseId)
    flushNow()
    updateSession(caseId, (session) => ({
      ...session,
      items: [...session.items, { kind: 'user', id: nextId('user'), text, files }],
    }))
  },

  completeStage: (caseId, stageId, result) => {
    updateSession(caseId, (session) => setStageStatus(session, stageId, 'completed', result))
  },

  skipStage: (caseId, stageId) => {
    updateSession(caseId, (session) => setStageStatus(session, stageId, 'skipped'))
  },

  seedMessages: (caseId, messages) => {
    get().ensureSession(caseId)
    const session = get().sessions[caseId]
    if (!session || session.items.length > 0 || messages.length === 0) return null

    let maxSeq = 0
    for (const msg of messages) {
      if (typeof msg.seq === 'number' && msg.seq > maxSeq) maxSeq = msg.seq
      switch (msg.role) {
        case 'user': {
          const files = (msg.meta?.files as FileMeta[] | undefined) ?? undefined
          updateSession(caseId, (s) => ({
            ...s,
            items: [...s.items, { kind: 'user', id: `msg-${msg.id}`, text: msg.content, files }],
          }))
          break
        }
        case 'assistant': {
          updateSession(caseId, (s) => ({
            ...s,
            items: [
              ...s.items,
              {
                kind: 'assistant',
                id: `msg-${msg.id}`,
                markdown: msg.content,
                streaming: false,
                stepKey: msg.step_key,
              },
            ],
          }))
          break
        }
        case 'event': {
          // 事件行：meta.event = 事件名，content = JSON 载荷 → 复用 reducer 重建状态
          const eventName = msg.meta?.event
          if (typeof eventName === 'string' && eventName) {
            reduceEvent(caseId, eventName, safeParseJson(msg.content))
            flushNow()
          }
          break
        }
        case 'system': {
          updateSession(caseId, (s) => pushNotice(s, msg.content))
          break
        }
      }
    }
    return maxSeq > 0 ? String(maxSeq) : null
  },

  hydratePipeline: (caseId, state) => {
    get().ensureSession(caseId)
    updateSession(caseId, (session) => {
      // 心跳每 5 秒一拍：刷新后先用快照把进度填上，别让最长的那一步显示成空白
      let next = state.progress
        ? { ...session, progress: state.progress, progressAt: Date.now() }
        : session
      for (const step of state.steps) {
        next = upsertStep(next, {
          step_key: step.key,
          status: step.status,
          name_zh: step.name_zh,
          attempt: step.attempt,
          error: step.error ?? null,
        })
      }
      /*
       * 门控状态校正（刷新恢复）。
       * 快照 replay 会把历史上每一条 interaction_required 重新推成 stage 卡，
       * reducer 只保证「最新一张是 active」——于是一个早已应答完、后端
       * pending_interaction 已经是 null 的门控，刷新后仍挂着 amber「待确认」
       * 和可点的确认钮，用户对着一个结束了的门控反复点，只会拿到 409。
       * 以后端为准：不在 pending_interaction（也不在 waiting_user 步骤）里的
       * active 卡一律收敛为 completed。
       */
      const pending = state.pending_interaction
      const waitingKeys = new Set(
        state.steps.filter((s) => s.status === 'waiting_user').map((s) => s.key),
      )
      for (const item of next.items) {
        if (item.kind !== 'stage' || item.stage.status !== 'active') continue
        const stepKey = item.stage.stepKey
        const stillOpen =
          (pending != null && stepKey === pending.step_key) ||
          (stepKey != null && waitingKeys.has(stepKey))
        if (!stillOpen) next = setStageStatus(next, item.stage.id, 'completed')
      }

      // 待确认交互：无同 step_key 的 active stage 时补一张卡
      if (pending) {
        const exists = next.items.some(
          (item) =>
            item.kind === 'stage' &&
            item.stage.status === 'active' &&
            item.stage.stepKey === pending.step_key,
        )
        if (!exists) {
          const stage: Stage = {
            id: nextId('stage'),
            type: pending.kind,
            status: 'active',
            payload: pending,
            stepKey: pending.step_key,
          }
          next = { ...next, items: [...next.items, { kind: 'stage', id: stage.id, stage }] }
        }
      }
      return next
    })
  },

  hydrateDocContent: (caseId, docId, patch) => {
    get().ensureSession(caseId)
    flushNow() // 先把在途 delta 落盘，避免拿到过期的「空正文」判断
    updateSession(caseId, (session) => {
      const current = session.docs[docId]
      // 实时流式优先：正在推 doc 通道 / 已有正文时一律不覆盖
      if (current && (current.streaming || current.markdown.length > 0)) return session
      const next = ensureDoc(session, docId)
      const doc = next.docs[docId]
      return {
        ...next,
        docs: {
          ...next.docs,
          [docId]: {
            ...doc,
            markdown: patch.markdown,
            streaming: false,
            hydrated: true,
            // 版本 / 下载信息以 doc_version 事件为准，缺失时才用交付物补齐
            versionId: doc.versionId ?? patch.versionId,
            version: doc.version ?? patch.version,
            filename: doc.filename ?? patch.filename,
            artifactId: doc.artifactId ?? patch.artifactId,
          },
        },
      }
    })
  },
}))

/** 选取单案会话（未建立时返回 undefined）。 */
export function useCaseSession(caseId: string): CaseSessionState | undefined {
  return useSessionStore((state) => state.sessions[caseId])
}
