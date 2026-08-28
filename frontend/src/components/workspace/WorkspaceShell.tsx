import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowDownIcon, ExclamationTriangleIcon } from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import {
  artifactDownloadUrl,
  availableFormats,
  downloadText,
  downloadUrl,
  type DownloadFormat,
} from '../../lib/download'
import { caseEventsUrl, createSseClient, type SseClient, type SseTransport } from '../../lib/sse'
import {
  useArtifactContent,
  useCancelPipeline,
  useCaseDetail,
  useCaseMessages,
  usePipelineState,
  useResumePipeline,
  useRetryPipeline,
  useStartPipeline,
  useSubmitInput,
} from '../../api/pipeline'
import { artifactToMarkdown, pickDocArtifact } from '../../lib/docHydration'
import { MediaCaseContext } from '../../lib/mediaContext'
import {
  DEFAULT_DOC_ID,
  useSessionStore,
  type CaseSessionState,
  type DocState,
} from '../../stores/sessionStore'
import { useComposerStore, type ComposerAttachment } from '../../stores/composerStore'
import { useUiStore } from '../../stores/uiStore'
import { ApiError } from '../../lib/api'
import { Button } from '../ui/Button'
import { EmptyState } from '../ui/EmptyState'
import { Skeleton } from '../ui/Skeleton'
import { Composer } from '../composer/Composer'
import { DocumentPanel } from '../document/DocumentPanel'
import type { VersionEntry } from '../document/VersionHistory'
import { PipelineStream } from '../pipeline/PipelineStream'
import {
  StepProgress,
  computeDisplaySteps,
  type DisplayStep,
  type DisplayStepPreset,
} from '../pipeline/StepProgress'
import { LiveProgress } from '../pipeline/LiveProgress'
import { CrossUserBanner } from './CrossUserBanner'
import { TierToggle } from '../composer/TierToggle'
import { useSetCaseModelTier, type ModelTier } from '../../api/sessions'
import type { InteractionRequiredEvent } from '../../types/stream'
import type { PipelineStepState } from '../../stores/sessionStore'

export interface WorkspaceShellProps {
  caseId: string
  /** §2.8 展示步骤集（四模块各一套）。 */
  stepPresets: DisplayStepPreset[]
  /**
   * 步骤条自定义解析（覆盖 stepPresets 的静态映射）：
   * 步骤集随分支动态增减的模块（如交底书实用/外观多一步 schema_fill）
   * 传 `computeDynamicSteps` 的偏应用，以后端 pipeline.steps 为准渲染。
   */
  stepResolver?: (pipelineSteps: PipelineStepState[]) => DisplayStep[]
  /** Composer / tile accent（论文转专利 = orange）。 */
  accent?: 'indigo' | 'orange'
  composerPlaceholder?: string
  /** Composer 附件 accept。 */
  fileAccept?: string
  /** 文档面板与流内文档卡标题。 */
  docTitle?: string
  /** 空会话态（mini-hero）。 */
  emptyState?: ReactNode
  /** mock 模式指定回放剧本（VITE_USE_MOCKS=1 时生效）。 */
  mockScript?: 'disclosure' | 'paper' | 'reader' | 'oa'
  /**
   * 覆盖右侧文档面板（缺省 DocumentPanel）。
   * 解读模块在此位置渲染 ReportPanel（ReportToc + report.json blocks），
   * 由页面自行订阅 sessionStore 取文档与版本。
   */
  documentPanel?: ReactNode
  /**
   * 首次进入自动 POST /pipeline/start 的载荷；
   * 仅在「会话流为空且流水线未跑过」时触发一次。
   */
  autoStartPayload?: Record<string, unknown> | null
  /** 自动启动成功后回调（清理路由 state 等）。 */
  onAutoStarted?: () => void
  /**
   * chat composer 发送；缺省行为 = 无进行中流水线时以
   * `{instruction, file_ids}` 启动新一轮。
   */
  onSend?: (text: string, fileIds: string[]) => void | Promise<void>
}

function extOf(filename: string | undefined): string {
  if (!filename) return ''
  const dot = filename.lastIndexOf('.')
  return dot >= 0 ? filename.slice(dot + 1).toLowerCase() : ''
}

/** 单条 doc_version 内按格式找交付物。 */
function pickDownload(
  doc: DocState | null,
  format: string,
): { artifactId: string; filename: string } | null {
  const hit = doc?.downloads?.find(
    (d) => String(d.kind).endsWith(format) || extOf(d.filename) === format,
  )
  if (hit) return { artifactId: hit.artifact_id, filename: hit.filename }
  if (doc?.artifactId && (extOf(doc.filename) === format || (!doc.filename && format === 'md'))) {
    return { artifactId: doc.artifactId, filename: doc.filename ?? `document.${format}` }
  }
  return null
}

/**
 * §3.2 通用工作台外壳（四模块复用）：
 * 左流式列（StepProgress 横幅 + PipelineStream + chat Composer 底部吸附 +
 * 吸底滚动 / 回到底部 FAB / 连接状态提示条）+ xl 右侧 DocumentPanel。
 *
 * 进入流程：拉 case 详情与 messages 快照 → seed 会话 → 建 SSE 连接
 * （VITE_USE_MOCKS=1 走 mock transport）→ sessionStore 驱动渲染。
 */
export function WorkspaceShell({
  caseId,
  stepPresets,
  stepResolver,
  accent = 'indigo',
  composerPlaceholder = zh.workspace.composerPlaceholder,
  fileAccept,
  docTitle,
  emptyState,
  mockScript,
  documentPanel,
  autoStartPayload = null,
  onAutoStarted,
  onSend,
}: WorkspaceShellProps) {
  const session = useSessionStore((s) => s.sessions[caseId]) as CaseSessionState | undefined
  const ensureSession = useSessionStore((s) => s.ensureSession)
  const seedMessages = useSessionStore((s) => s.seedMessages)
  const hydratePipeline = useSessionStore((s) => s.hydratePipeline)
  const hydrateDocContent = useSessionStore((s) => s.hydrateDocContent)
  const handleSseFrame = useSessionStore((s) => s.handleSseFrame)
  const setConnection = useSessionStore((s) => s.setConnection)
  const pushUserMessage = useSessionStore((s) => s.pushUserMessage)
  const completeStage = useSessionStore((s) => s.completeStage)
  const skipStage = useSessionStore((s) => s.skipStage)
  const pushToast = useUiStore((s) => s.pushToast)
  const navigate = useNavigate()

  const detailQuery = useCaseDetail(caseId)
  const messagesQuery = useCaseMessages(caseId)
  const pipelineQuery = usePipelineState(caseId)
  const startPipeline = useStartPipeline(caseId)
  const submitInput = useSubmitInput(caseId)
  const cancelPipeline = useCancelPipeline(caseId)
  const retryPipeline = useRetryPipeline(caseId)
  const resumePipeline = useResumePipeline(caseId)

  const [snapshotReady, setSnapshotReady] = useState(false)
  const [connectEpoch, setConnectEpoch] = useState(0)
  const [panelCollapsed, setPanelCollapsed] = useState(false)
  const [showFab, setShowFab] = useState(false)
  const [busyStageId, setBusyStageId] = useState<string | null>(null)

  // --- 模型档位 -------------------------------------------------------------
  const setTier = useSetCaseModelTier()
  /*
   * 乐观值：点下去先动，服务端确认后再交回服务端状态。
   * 纯受控（只认 detailQuery）的话，从点击到 refetch 回来这段时间开关一动不动，
   * 用起来像没点上——而这个开关恰恰是「点一下看它动一下」才成立的控件。
   * 失败时清掉乐观值，开关自己弹回真实档位，同时弹错误提示。
   */
  const [tierOptimistic, setTierOptimistic] = useState<ModelTier | null>(null)
  const serverTier: ModelTier =
    (detailQuery.data?.state?.['_model_tier'] as ModelTier | undefined) ?? 'deep'
  const caseTier: ModelTier = tierOptimistic ?? serverTier

  // 服务端追上来之后放开乐观值，之后一切以服务端为准
  useEffect(() => {
    if (tierOptimistic !== null && serverTier === tierOptimistic) setTierOptimistic(null)
  }, [serverTier, tierOptimistic])

  const handleTierChange = useCallback(
    (next: ModelTier) => {
      if (!caseId || next === caseTier) return
      setTierOptimistic(next)
      setTier.mutate(
        { id: caseId, tier: next },
        {
          onSuccess: () => {
            // 说清「只影响之后的步骤」——不说的话，用户会以为切一下就能让上一步重来
            pushToast(
              'success',
              `${zh.tier.switched(next === 'fast' ? zh.tier.fast : zh.tier.deep)}　${zh.tier.appliesNext}`,
            )
          },
          onError: () => {
            setTierOptimistic(null)
            pushToast('error', zh.tier.switchFailed)
          },
        },
      )
    },
    [caseId, caseTier, setTier, pushToast],
  )

  const seededRef = useRef(false)
  const hydratedRef = useRef(false)
  /** 已回填过正文的案件（回填一次即可，之后以 SSE 为准）。 */
  const docHydratedRef = useRef<string | null>(null)
  /** 已自动启动过流水线的案件（StrictMode 双挂载下不重复 start）。 */
  const autoStartedCaseRef = useRef<string | null>(null)
  /** 已自动恢复过的案件（同上，且 resume 只需一次）。 */
  const autoResumedCaseRef = useRef<string | null>(null)
  const cursorRef = useRef<string | null>(null)
  const scrollerRef = useRef<HTMLDivElement>(null)
  const atBottomRef = useRef(true)

  // --- 案件切换：重置一次性标记 ---------------------------------------------
  useEffect(() => {
    seededRef.current = false
    hydratedRef.current = false
    docHydratedRef.current = null
    autoResumedCaseRef.current = null
    cursorRef.current = null
    atBottomRef.current = true
    setSnapshotReady(false)
    if (caseId) ensureSession(caseId)
  }, [caseId, ensureSession])

  // --- 1) messages 快照种流（拿到续传游标后才建 SSE） -------------------------
  const snapshotSettled = messagesQuery.isSuccess || messagesQuery.isError
  useEffect(() => {
    if (!caseId || !snapshotSettled || seededRef.current) return
    seededRef.current = true
    // 案件本身就是 404 时不再弹「快照加载失败」：页面已经给出「会话不存在」，
    // 再压一条「将以实时事件为准」只会让人以为还有救。
    const notFound =
      messagesQuery.error instanceof ApiError && messagesQuery.error.status === 404
    if (messagesQuery.isError && !notFound) pushToast('warning', zh.workspace.snapshotLoadFailed)
    if (messagesQuery.data && messagesQuery.data.length > 0) {
      cursorRef.current = seedMessages(caseId, messagesQuery.data)
    }
    setSnapshotReady(true)
  }, [
    caseId,
    snapshotSettled,
    messagesQuery.data,
    messagesQuery.isError,
    messagesQuery.error,
    seedMessages,
    pushToast,
  ])

  /*
   * --- 2) 流水线状态水合（刷新恢复步骤条、待确认交互、门控卡状态） ------------
   * 必须等 seed 完成再跑：门控状态校正要改的正是快照 replay 出来的那批 stage 卡，
   * 抢在 seedMessages 之前水合的话，卡片还没进流，校正就落空了（已应答的门控
   * 又会挂着「待确认」）。
   */
  useEffect(() => {
    if (!caseId || hydratedRef.current || !snapshotReady || !pipelineQuery.data) return
    hydratedRef.current = true
    hydratePipeline(caseId, pipelineQuery.data)
  }, [caseId, snapshotReady, pipelineQuery.data, hydratePipeline])

  /*
   * 案件不存在（GET /cases/{id} → 404）。
   * 不加这条判定时，失效链接会渲染出一整套「新会话」空态：步骤条、上传区、
   * 输入框全都在，只有一条「连接中断，正在重连」的黄条——用户看不出案件已经没了，
   * 还会继续往一个不存在的案件里发消息。故 404 单独走不可恢复的空态。
   */
  const caseMissing =
    detailQuery.isError && detailQuery.error instanceof ApiError && detailQuery.error.status === 404

  /*
   * --- 2b) 文档正文回填（刷新恢复右侧面板） ----------------------------------
   * docs[docId].markdown 只由实时 SSE 的 llm_delta(channel='doc') 累积，而 delta
   * 不入 messages 表 —— 案件跑完后重新打开，面板就只剩一个空的 .md-prose，只有
   * 下载能用。这里从「各 kind 最新版交付物」（GET /cases/{id} 的 artifacts_latest）
   * 里挑出本模块的正文交付物，取 GET /artifacts/{id}/content 回填。
   *
   * 实时流式优先：目标文档已有正文或正在流式时不发请求；万一回填后新一轮 doc
   * 流式才开始，sessionStore 的 llm_delta 分支会在首个 delta 到达时清掉回填正文。
   */
  const hydrationDocId = session?.activeDocId ?? DEFAULT_DOC_ID
  const hydrationDoc = session?.docs[hydrationDocId] ?? null
  const needsDocHydration =
    snapshotReady &&
    !caseMissing &&
    docHydratedRef.current !== caseId &&
    !hydrationDoc?.streaming &&
    !hydrationDoc?.markdown

  const docArtifact = useMemo(
    () =>
      needsDocHydration
        ? pickDocArtifact(detailQuery.data?.module, detailQuery.data?.artifacts_latest, hydrationDoc)
        : null,
    [needsDocHydration, detailQuery.data, hydrationDoc],
  )

  const docContentQuery = useArtifactContent(docArtifact?.id, needsDocHydration)
  const docContent = docContentQuery.data

  useEffect(() => {
    // content 非字符串 = 拿到的不是交付物正文（mock 透传 / 代理返回 HTML），直接放弃
    if (!caseId || !needsDocHydration || typeof docContent?.content !== 'string') return
    docHydratedRef.current = caseId
    hydrateDocContent(caseId, hydrationDocId, {
      markdown: artifactToMarkdown(docContent.filename, docContent.content),
      versionId: docContent.id,
      version: docContent.version,
      filename: docContent.filename,
      artifactId: docContent.id,
    })
  }, [caseId, docContent, needsDocHydration, hydrationDocId, hydrateDocContent])

  // --- 3) SSE 连接（mock 模式换 transport） ----------------------------------
  useEffect(() => {
    if (!caseId || !snapshotReady || caseMissing) return
    let cancelled = false
    let client: SseClient | null = null

    const boot = async (): Promise<void> => {
      let transport: SseTransport | undefined
      // 内联 env 判断（见 lib/sse.ts isMockMode 注释）：生产构建折为死代码
      if (import.meta.env.VITE_USE_MOCKS === '1') {
        const mocks = await import('../../mocks/mockServer')
        mocks.installMockApi()
        transport = mocks.createMockSseTransport(
          mockScript ? { script: mocks.getMockScript(mockScript) } : {},
        )
      }
      if (cancelled) return
      client = createSseClient({
        url: caseEventsUrl(caseId),
        transport,
        initialLastEventId: cursorRef.current,
        onEvent: (frame) => handleSseFrame(caseId, frame),
        onStateChange: (state, info) => setConnection(caseId, state, info.attempt),
      })
      client.start()
    }
    void boot()

    return () => {
      cancelled = true
      client?.close()
    }
  }, [caseId, snapshotReady, caseMissing, connectEpoch, mockScript, handleSseFrame, setConnection])

  // --- 4) 首次进入自动启动流水线 --------------------------------------------
  const pipelineSettled = pipelineQuery.isSuccess || pipelineQuery.isError

  // --- 重启后自动恢复停在门控的案件 ------------------------------------------
  //
  // 服务重启（部署更新、看门狗拉起）会清空引擎的内存态，但数据库里的
  // waiting_user 保持原样——恢复责任被交给了 /pipeline/resume。而在此之前
  // 前端从没调过它：门控卡片照常渲染成可点，用户一点得到 409，
  // cancel 和 retry 同样 409，于是这个案件彻底卡死，几十分钟的产出只能删案重来。
  //
  // resume 对 waiting_user 的语义是「重发 interaction_required」，不重跑 LLM，
  // 所以这里自动做掉而不是让用户点一颗「继续」按钮——没人该知道服务重启过。
  useEffect(() => {
    if (!caseId || !pipelineSettled || caseMissing) return
    if (autoResumedCaseRef.current === caseId) return
    const state = pipelineQuery.data
    if (!state) return
    const waiting = (state.steps ?? []).some((s) => s.status === 'waiting_user')
    if (!waiting || state.pending_interaction) return
    autoResumedCaseRef.current = caseId
    void resumePipeline.mutateAsync().catch(() => {
      // 恢复不了不该打断阅读：卡片仍在，用户可以走「取消/重试」或联系管理员
    })
  }, [caseId, pipelineSettled, caseMissing, pipelineQuery.data, resumePipeline])

  const itemCount = session?.items.length ?? 0
  useEffect(() => {
    if (!caseId || !autoStartPayload || autoStartedCaseRef.current === caseId) return
    if (!snapshotReady || !pipelineSettled || caseMissing) return
    autoStartedCaseRef.current = caseId
    const alreadyStarted = (pipelineQuery.data?.steps ?? []).some((s) => s.status !== 'pending')
    if (itemCount > 0 || alreadyStarted) return
    void (async () => {
      try {
        await startPipeline.mutateAsync(autoStartPayload)
        onAutoStarted?.()
      } catch (e) {
        const message = e instanceof Error ? e.message : ''
        pushToast('error', message || zh.workspace.startFailed)
      }
    })()
    // startPipeline / itemCount 仅作首次判定，不参与依赖以免重复触发
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [caseId, autoStartPayload, snapshotReady, pipelineSettled, caseMissing])

  // --- 吸底滚动 + 回到底部 FAB ----------------------------------------------
  const items = session?.items
  const docs = session?.docs
  useEffect(() => {
    const el = scrollerRef.current
    if (el && atBottomRef.current) el.scrollTop = el.scrollHeight
  }, [items, docs])

  const onScroll = useCallback(() => {
    const el = scrollerRef.current
    if (!el) return
    const dist = el.scrollHeight - el.scrollTop - el.clientHeight
    atBottomRef.current = dist < 48
    setShowFab(dist > 120)
  }, [])

  const scrollToBottom = useCallback(() => {
    const el = scrollerRef.current
    if (!el) return
    atBottomRef.current = true
    el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
  }, [])

  // --- HITL 提交 -------------------------------------------------------------
  const stageStepKey = useCallback(
    (stageId: string): string => {
      const item = session?.items.find((i) => i.kind === 'stage' && i.stage.id === stageId)
      if (!item || item.kind !== 'stage') return ''
      const payload = item.stage.payload as InteractionRequiredEvent | null
      return item.stage.stepKey ?? payload?.step_key ?? String(item.stage.type)
    },
    [session?.items],
  )

  const handleStageSubmit = useCallback(
    (stageId: string, payload: unknown) => {
      const stepKey = stageStepKey(stageId)
      setBusyStageId(stageId)
      completeStage(caseId, stageId, payload) // 乐观 completed（§4）
      void submitInput
        .mutateAsync({ step_key: stepKey, payload })
        .catch((e: unknown) => {
          const message = e instanceof Error ? e.message : ''
          pushToast('error', message || zh.workspace.inputSubmitFailed)
        })
        .finally(() => setBusyStageId(null))
    },
    [caseId, completeStage, pushToast, stageStepKey, submitInput],
  )

  const handleStageSkip = useCallback(
    (stageId: string) => {
      const stepKey = stageStepKey(stageId)
      setBusyStageId(stageId)
      skipStage(caseId, stageId)
      void submitInput
        .mutateAsync({ step_key: stepKey, payload: { skipped: true } })
        .catch((e: unknown) => {
          const message = e instanceof Error ? e.message : ''
          pushToast('error', message || zh.workspace.skipFailed)
        })
        .finally(() => setBusyStageId(null))
    },
    [caseId, pushToast, skipStage, stageStepKey, submitInput],
  )

  // --- 步骤重试（流内失败卡 / 顶部提示条共用） ------------------------------
  const handleRetryStep = useCallback(
    (stepKey?: string) => {
      void retryPipeline
        .mutateAsync(stepKey)
        .then(() => pushToast('info', zh.pipeline.retryRequested))
        .catch((e: unknown) => {
          const message = e instanceof Error ? e.message : ''
          pushToast('error', message || zh.pipeline.retryFailed)
        })
    },
    [pushToast, retryPipeline],
  )

  // --- 流水线运行态 / 停止 ---------------------------------------------------
  const running = useMemo(
    () =>
      !session?.pipeline.done &&
      (session?.pipeline.steps ?? []).some((s) => s.status === 'running'),
    [session?.pipeline.done, session?.pipeline.steps],
  )

  const handleStop = useCallback(() => {
    void cancelPipeline
      .mutateAsync()
      .then(() => pushToast('info', zh.workspace.cancelRequested))
      .catch((e: unknown) => {
        const message = e instanceof Error ? e.message : ''
        pushToast('error', message || zh.workspace.cancelFailed)
      })
  }, [cancelPipeline, pushToast])

  // --- chat composer 发送 ----------------------------------------------------
  const handleSend = useCallback(
    (text: string, attachments: ComposerAttachment[]) => {
      const fileIds = attachments
        .map((a) => a.fileId)
        .filter((id): id is string => Boolean(id))
      pushUserMessage(
        caseId,
        text,
        attachments.map((a) => ({ id: a.fileId, name: a.name, size: a.size })),
      )
      useComposerStore.getState().clearContext(caseId)

      if (onSend) {
        void onSend(text, fileIds)
        return
      }
      if (running) {
        pushToast('info', zh.workspace.alreadyRunning)
        return
      }
      void startPipeline
        .mutateAsync({ instruction: text, file_ids: fileIds })
        .catch((e: unknown) => {
          const message = e instanceof Error ? e.message : ''
          pushToast('error', message || zh.workspace.startFailed)
        })
    },
    [caseId, onSend, pushToast, pushUserMessage, running, startPipeline],
  )

  // --- 文档面板数据 ----------------------------------------------------------
  const activeDocId = session?.activeDocId ?? null
  const activeDoc = activeDocId ? (session?.docs[activeDocId] ?? null) : null
  const panelTitle =
    session?.title ?? detailQuery.data?.title ?? docTitle ?? zh.document.defaultDocTitle

  const versions = useMemo<VersionEntry[]>(
    () =>
      (session?.versions ?? [])
        .filter((v) => (v.doc_id ?? 'main') === (activeDocId ?? 'main'))
        .map((v) => ({
          id: v.version_id,
          version: v.version,
          filename: v.filename,
          createdAt: v.created_at,
          iterationType: v.iteration_type ?? null,
          formats: availableFormats(v.downloads, v.filename),
        }))
        .reverse(),
    [session?.versions, activeDocId],
  )

  const handleDownload = useCallback(
    (format: DownloadFormat) => {
      const hit = pickDownload(activeDoc, format)
      if (hit) {
        downloadUrl(artifactDownloadUrl(hit.artifactId), hit.filename)
        return
      }
      if (format === 'md' && activeDoc?.markdown) {
        downloadText(
          activeDoc.markdown,
          activeDoc.filename ?? 'document.md',
          'text/markdown;charset=utf-8',
        )
        return
      }
      pushToast('info', zh.workspace.downloadUnavailable(format))
    },
    [activeDoc, pushToast],
  )

  const handleVersionDownload = useCallback(
    (entry: VersionEntry, format: DownloadFormat) => {
      const version = (session?.versions ?? []).find((v) => v.version_id === entry.id)
      const hit = version?.downloads?.find(
        (d) => String(d.kind).endsWith(format) || extOf(d.filename) === format,
      )
      const artifactId =
        hit?.artifact_id ?? (extOf(entry.filename) === format ? version?.artifact_id : undefined)
      if (!artifactId) {
        pushToast('info', zh.workspace.downloadUnavailable(format))
        return
      }
      downloadUrl(artifactDownloadUrl(artifactId), hit?.filename ?? entry.filename)
    },
    [pushToast, session?.versions],
  )

  // --- 连接 / 错误提示条 ------------------------------------------------------
  const connection = session?.connection ?? 'idle'
  const attempt = session?.connectionAttempt ?? 0
  const lastError = session?.lastError ?? null
  const connectionBad = connection === 'reconnecting' || connection === 'failed'
  const bannerText = connectionBad
    ? connection === 'reconnecting' && attempt > 0
      ? zh.connection.reconnectingAttempt(attempt)
      : zh.connection[connection]
    : lastError?.message

  const bannerAction = connectionBad
    ? { label: zh.workspace.reconnect, run: () => setConnectEpoch((v) => v + 1) }
    : lastError?.retryable
      ? { label: zh.common.retry, run: () => handleRetryStep(undefined) }
      : null

  const pipelineSteps = session?.pipeline.steps ?? []
  const displaySteps = stepResolver
    ? stepResolver(pipelineSteps)
    : computeDisplaySteps(stepPresets, pipelineSteps)
  const loading = !snapshotReady && itemCount === 0

  if (caseMissing) {
    return (
      <div className="flex-1 min-h-0 flex items-center justify-center p-6">
        <EmptyState
          icon={ExclamationTriangleIcon}
          title={zh.workspace.notFoundTitle}
          description={zh.workspace.notFoundDesc}
          action={
            <Button variant="secondary" size="sm" onClick={() => navigate('/')}>
              {zh.workspace.backToHome}
            </Button>
          }
        />
      </div>
    )
  }

  return (
    /* 正文里的插图是磁盘路径，MarkdownBlock 要靠这个案件 id 换成媒体端点才取得到图 */
    <MediaCaseContext.Provider value={caseId ?? null}>
    <div className="flex-1 min-h-0 flex overflow-hidden">
      {/* 左：流式列 */}
      <div className="flex-1 min-w-0 flex flex-col relative">
        {/* 管理员查看他人案件：置顶 amber 提示（§4.2，后端已记 cross_user_read 审计） */}
        <CrossUserBanner caseDetail={detailQuery.data} />
        <StepProgress steps={displaySteps} />
        {/* 步骤条只答「第几步」；这条答「这一步在做什么、跑了多久、是不是卡住了」 */}
        <LiveProgress
          progress={session?.progress ?? null}
          progressAt={session?.progressAt ?? null}
          connection={session?.connection ?? 'idle'}
        />

        {bannerText && (
          <div
            className={cn(
              'shrink-0 px-4 py-2 border-b flex items-center gap-2 text-xs',
              connectionBad || lastError
                ? 'border-amber-100 dark:border-amber-500/20 bg-amber-50 dark:bg-amber-500/10 text-amber-700 dark:text-amber-400'
                : 'border-gray-100 dark:border-gray-800 text-gray-500',
            )}
            role="status"
          >
            <ExclamationTriangleIcon className="w-4 h-4 shrink-0" strokeWidth={1.5} />
            <span className="truncate flex-1 min-w-0">{bannerText}</span>
            {bannerAction && (
              <Button variant="ghost" size="sm" onClick={bannerAction.run} className="shrink-0">
                {bannerAction.label}
              </Button>
            )}
          </div>
        )}

        <div ref={scrollerRef} onScroll={onScroll} className="flex-1 min-h-0 overflow-y-auto">
          {loading ? (
            <div className="max-w-[800px] mx-auto px-4 py-6 space-y-4">
              <Skeleton shimmer className="h-24 rounded-2xl" />
              <Skeleton shimmer className="h-24 rounded-2xl" />
              <Skeleton shimmer className="h-24 rounded-2xl" />
            </div>
          ) : (
            <PipelineStream
              caseId={caseId}
              session={session}
              docTitle={panelTitle}
              emptyState={emptyState}
              onStageSubmit={handleStageSubmit}
              onStageSkip={handleStageSkip}
              busyStageId={busyStageId}
              onRetryStep={handleRetryStep}
              retrying={retryPipeline.isPending}
              showDisclaimer={false}
            />
          )}
        </div>

        {showFab && (
          <button
            type="button"
            onClick={scrollToBottom}
            aria-label={zh.workspace.backToBottom}
            title={zh.workspace.backToBottom}
            className={cn(
              'absolute bottom-32 right-6 z-[var(--z-fab)] w-9 h-9 rounded-full',
              'glass-effect shadow-lg flex items-center justify-center',
              'text-gray-600 dark:text-gray-300 hover:scale-105 transition-transform',
              'animate-in fade-in zoom-in-95 duration-200',
            )}
          >
            <ArrowDownIcon className="w-4 h-4" strokeWidth={2} />
          </button>
        )}

        <Composer
          contextId={caseId}
          variant="chat"
          accent={accent}
          caseId={caseId}
          fileAccept={fileAccept}
          placeholder={composerPlaceholder}
          busy={running}
          onStop={handleStop}
          onSend={handleSend}
          toolbarRight={
            <TierToggle
              value={caseTier}
              onChange={handleTierChange}
              busy={setTier.isPending}
            />
          }
        />
      </div>

      {/* 右：文档面板（xl 以上；§3.2）；解读模块经 documentPanel 换成报告面板 */}
      {documentPanel !== undefined ? (
        documentPanel
      ) : (
        <DocumentPanel
          className={cn('hidden xl:flex', panelCollapsed ? '' : 'w-[46%] max-w-[720px]')}
          title={panelTitle}
          doc={activeDoc}
          versions={versions}
          onDownload={handleDownload}
          onVersionDownload={handleVersionDownload}
          collapsed={panelCollapsed}
          onToggleCollapse={() => setPanelCollapsed((v) => !v)}
        />
      )}
    </div>
    </MediaCaseContext.Provider>
  )
}
