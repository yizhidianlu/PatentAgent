import { useCallback, useEffect, useRef, useState } from 'react'
import { ArrowDownIcon, PlayIcon } from '@heroicons/react/24/outline'
import { cn } from '../lib/cn'
import { api } from '../lib/api'
import { downloadText, type DownloadFormat } from '../lib/download'
import { caseEventsUrl, createSseClient, type SseClient } from '../lib/sse'
import { createMockSseTransport, installMockApi, setMockSpeed } from '../mocks/mockServer'
import { useSessionStore } from '../stores/sessionStore'
import { useUiStore } from '../stores/uiStore'
import { zh } from '../i18n/zh'
import type { InteractionRequiredEvent } from '../types/stream'
import { Badge } from '../components/ui/Badge'
import { Button } from '../components/ui/Button'
import { SegmentedToggle } from '../components/ui/SegmentedToggle'
import { DocumentPanel } from '../components/document/DocumentPanel'
import type { VersionEntry } from '../components/document/VersionHistory'
import { PipelineStream } from '../components/pipeline/PipelineStream'

type ScriptKey = 'disclosure' | 'paper'

const CASE_IDS: Record<ScriptKey, string> = {
  disclosure: 'demo-disclosure',
  paper: 'demo-paper',
}

const USER_OPENERS: Record<ScriptKey, string> = {
  disclosure: '我们做了一个便携式术后康复监测装置，想写一份交底书。',
  paper: '这是我们的论文《Graph-based Lesion Segmentation》，请转成专利申请。',
}

const SCRIPT_OPTIONS = [
  { value: 'disclosure', label: zh.demo.scripts.disclosure },
  { value: 'paper', label: zh.demo.scripts.paper },
] as const

const SPEED_OPTIONS = [
  { value: '1', label: '1x' },
  { value: '2', label: '2x' },
  { value: '4', label: '4x' },
] as const

/**
 * dev-only /stream-demo：mock transport 一键回放两条剧本，
 * 验证流式渲染 / mermaid 闭合渲染 / rAF 刷新 / 吸底滚动 + 回到底部 FAB /
 * StageCard 注册表分发 / 文档面板。
 */
export function StreamDemoPage() {
  const [script, setScript] = useState<ScriptKey>('disclosure')
  const [speed, setSpeed] = useState<'1' | '2' | '4'>('2')
  const [playing, setPlaying] = useState(false)
  const [panelCollapsed, setPanelCollapsed] = useState(false)
  const [showFab, setShowFab] = useState(false)

  const caseId = CASE_IDS[script]
  const session = useSessionStore((s) => s.sessions[caseId])
  const resetSession = useSessionStore((s) => s.resetSession)
  const pushUserMessage = useSessionStore((s) => s.pushUserMessage)
  const handleSseFrame = useSessionStore((s) => s.handleSseFrame)
  const setConnection = useSessionStore((s) => s.setConnection)
  const completeStage = useSessionStore((s) => s.completeStage)
  const skipStage = useSessionStore((s) => s.skipStage)
  const pushToast = useUiStore((s) => s.pushToast)

  const clientRef = useRef<SseClient | null>(null)
  const scrollerRef = useRef<HTMLDivElement>(null)
  const atBottomRef = useRef(true)

  // Mock API 拦截（POST /cases、/pipeline/* → 202；门控放行）
  useEffect(() => installMockApi(), [])

  // 卸载时断开
  useEffect(
    () => () => {
      clientRef.current?.close()
      clientRef.current = null
    },
    [],
  )

  // 速度即时生效（mock transport 逐事件读取速度因子）
  useEffect(() => {
    setMockSpeed(Number(speed))
  }, [speed])

  const play = useCallback(() => {
    clientRef.current?.close()
    resetSession(caseId)
    pushUserMessage(caseId, USER_OPENERS[script])
    setMockSpeed(Number(speed))

    const client = createSseClient({
      url: caseEventsUrl(caseId),
      transport: createMockSseTransport(),
      onEvent: (frame) => handleSseFrame(caseId, frame),
      onStateChange: (state, info) => setConnection(caseId, state, info.attempt),
    })
    clientRef.current = client
    client.start()
    setPlaying(true)
    atBottomRef.current = true
  }, [caseId, script, speed, resetSession, pushUserMessage, handleSseFrame, setConnection])

  const stopAndReset = useCallback(() => {
    clientRef.current?.close()
    clientRef.current = null
    resetSession(caseId)
    setPlaying(false)
  }, [caseId, resetSession])

  // 吸底滚动：位于底部时随内容增长自动滚到底
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

  // HITL：乐观置态 + POST /pipeline/input（mock 拦截并放行时间线）
  const postInput = useCallback(
    (stageId: string, payload: unknown) => {
      const item = session?.items.find((i) => i.kind === 'stage' && i.stage.id === stageId)
      if (!item || item.kind !== 'stage') return
      const raw = item.stage.payload as InteractionRequiredEvent | null
      const stepKey = item.stage.stepKey ?? raw?.step_key ?? String(item.stage.type)
      void api
        .post(`/cases/${caseId}/pipeline/input`, { step_key: stepKey, payload })
        .catch(() => {
          /* mock/后端不可达时不阻塞演示 */
        })
    },
    [caseId, session?.items],
  )

  const handleStageSubmit = useCallback(
    (stageId: string, payload: unknown) => {
      completeStage(caseId, stageId, payload)
      postInput(stageId, payload)
    },
    [caseId, completeStage, postInput],
  )

  const handleStageSkip = useCallback(
    (stageId: string) => {
      skipStage(caseId, stageId)
      postInput(stageId, { skipped: true })
    },
    [caseId, postInput, skipStage],
  )

  // 文档面板数据
  const activeDocId = session?.activeDocId ?? null
  const activeDoc = activeDocId ? (session?.docs[activeDocId] ?? null) : null
  const versions: VersionEntry[] = (session?.versions ?? [])
    .filter((v) => (v.doc_id ?? 'main') === (activeDocId ?? 'main'))
    .map((v) => ({
      id: v.version_id,
      version: v.version,
      filename: v.filename,
      createdAt: v.created_at,
      iterationType: v.iteration_type ?? null,
    }))
    .reverse()

  const handleDownload = useCallback(
    (format: DownloadFormat) => {
      if (format === 'md' && activeDoc) {
        downloadText(
          activeDoc.markdown,
          activeDoc.filename ?? 'document.md',
          'text/markdown;charset=utf-8',
        )
        return
      }
      pushToast('info', zh.demo.downloadUnavailable(format))
    },
    [activeDoc, pushToast],
  )

  const connection = session?.connection ?? 'idle'
  const connectionText =
    connection === 'reconnecting' && (session?.connectionAttempt ?? 0) > 0
      ? zh.connection.reconnectingAttempt(session?.connectionAttempt ?? 0)
      : zh.connection[connection]
  const connectionVariant =
    connection === 'open'
      ? ('emerald' as const)
      : connection === 'failed'
        ? ('red' as const)
        : connection === 'reconnecting' || connection === 'connecting'
          ? ('amber' as const)
          : ('neutral' as const)

  return (
    <div className="flex-1 min-h-0 flex overflow-hidden">
      {/* 左：消息流 */}
      <div className="flex-1 min-w-0 flex flex-col relative">
        {/* 工具条 */}
        <div className="shrink-0 px-4 py-3 border-b border-gray-200 dark:border-gray-800 bg-white/60 dark:bg-gray-900/60 backdrop-blur-md">
          <div className="max-w-[800px] mx-auto flex flex-wrap items-center gap-3">
            <div className="min-w-0">
              <h1 className="text-sm font-semibold text-gray-900 dark:text-gray-100">
                {zh.demo.title}
              </h1>
              <p className="text-xs text-gray-500 dark:text-gray-400 truncate">
                {zh.demo.subtitle}
              </p>
            </div>
            <div className="flex-1" />
            <SegmentedToggle
              options={SCRIPT_OPTIONS}
              value={script}
              onChange={(v) => setScript(v)}
            />
            <div className="flex items-center gap-1.5">
              <span className="text-xs text-gray-500 dark:text-gray-400">{zh.demo.speed}</span>
              <SegmentedToggle options={SPEED_OPTIONS} value={speed} onChange={(v) => setSpeed(v)} />
            </div>
            <Button size="sm" onClick={play} className="gap-1.5">
              <PlayIcon className="w-3.5 h-3.5" strokeWidth={2} />
              {playing ? zh.demo.replay : zh.demo.play}
            </Button>
            <Button size="sm" variant="secondary" onClick={stopAndReset}>
              {zh.demo.reset}
            </Button>
            <Badge variant={connectionVariant}>{connectionText}</Badge>
          </div>
        </div>

        {/* 滚动流 */}
        <div ref={scrollerRef} onScroll={onScroll} className="flex-1 min-h-0 overflow-y-auto">
          <PipelineStream
            caseId={caseId}
            session={session}
            docTitle={session?.title ?? zh.document.defaultDocTitle}
            onStageSubmit={handleStageSubmit}
            onStageSkip={handleStageSkip}
            emptyState={
              <div className="pt-16 text-center space-y-1.5">
                <p className="text-sm font-medium text-gray-700 dark:text-gray-300">
                  {zh.demo.emptyTitle}
                </p>
                <p className="text-xs text-gray-400 dark:text-gray-500">{zh.demo.emptyDesc}</p>
              </div>
            }
          />
        </div>

        {/* 回到底部 FAB */}
        {showFab && (
          <button
            type="button"
            onClick={scrollToBottom}
            aria-label={zh.demo.backToBottom}
            title={zh.demo.backToBottom}
            className={cn(
              'absolute bottom-6 right-6 z-[var(--z-fab)] w-9 h-9 rounded-full',
              'glass-effect shadow-lg flex items-center justify-center',
              'text-gray-600 dark:text-gray-300 hover:scale-105 transition-transform',
              'animate-in fade-in zoom-in-95 duration-200',
            )}
          >
            <ArrowDownIcon className="w-4 h-4" strokeWidth={2} />
          </button>
        )}
      </div>

      {/* 右：文档面板（xl 以上；§3.2） */}
      <DocumentPanel
        className={cn('hidden xl:flex', panelCollapsed ? '' : 'w-[46%] max-w-[720px]')}
        title={session?.title ?? zh.document.defaultDocTitle}
        doc={activeDoc}
        versions={versions}
        onDownload={handleDownload}
        onVersionDownload={(_entry, format) => handleDownload(format)}
        collapsed={panelCollapsed}
        onToggleCollapse={() => setPanelCollapsed((v) => !v)}
      />
    </div>
  )
}
