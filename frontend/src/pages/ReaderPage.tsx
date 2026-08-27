import { useCallback, useMemo } from 'react'
import { useLocation, useNavigate, useParams } from 'react-router-dom'
import { zh } from '../i18n/zh'
import { isMockMode } from '../lib/sse'
import { artifactDownloadUrl, downloadText, downloadUrl } from '../lib/download'
import { isLikelyPubNo, normalizeClaimTree, normalizePubNo, useReaderReport } from '../api/reader'
import { useStartPipeline } from '../api/pipeline'
import { useCaseSession } from '../stores/sessionStore'
import { useComposerStore } from '../stores/composerStore'
import { useUiStore } from '../stores/uiStore'
import { ReaderIntakeCard } from '../components/reader/ReaderIntakeCard'
import { ReportPanel } from '../components/reader/ReportPanel'
import { WorkspaceShell } from '../components/workspace/WorkspaceShell'
import type { DisplayStepPreset } from '../components/pipeline/StepProgress'
import type { InteractionRequiredEvent } from '../types/stream'

/**
 * §2.8 专利解读展示步骤集 → 后端 reader 步骤（backend-architecture.md §4 模块③）：
 * acquire → claim_tree(review) → claim_deltas → note(3 次调用 11 节) → lint_deliver。
 */
const READER_STEPS: DisplayStepPreset[] = [
  { key: 'acquire', label: zh.reader.steps.acquire },
  { key: 'claim_tree', label: zh.reader.steps.claim_tree },
  { key: 'claim_deltas', label: zh.reader.steps.claim_deltas },
  { key: 'note', label: zh.reader.steps.note },
  { key: 'lint_deliver', label: zh.reader.steps.lint_deliver },
]

const FILE_ACCEPT = '.pdf,application/pdf'

/** HomePage 跳转时携带的自动启动参数（解读模块首轮输入可能是公开号文本）。 */
interface ReaderLocationState {
  autoStart?: boolean
  fileIds?: string[]
  instruction?: string
}

/**
 * §3.4 ReaderPage：WorkspaceShell（indigo accent，5 步）
 * + 空会话「输入来源」卡（公开号 / PDF）
 * + 右侧 ReportPanel（ReportToc 常驻 + report.json blocks 渲染）。
 */
export function ReaderPage() {
  const { id } = useParams<{ id: string }>()
  const caseId = id ?? ''
  const location = useLocation()
  const navigate = useNavigate()
  const pushToast = useUiStore((s) => s.pushToast)

  const session = useCaseSession(caseId)
  const startPipeline = useStartPipeline(caseId)

  const routeState = (location.state ?? null) as ReaderLocationState | null

  const autoStartPayload = useMemo(() => {
    if (!routeState?.autoStart || isMockMode()) return null
    const fileIds = routeState.fileIds ?? []
    const instruction = routeState.instruction ?? ''
    const pubNo = normalizePubNo(instruction)
    return {
      pub_no: isLikelyPubNo(pubNo) ? pubNo : '',
      file_id: fileIds[0] ?? null,
      file_ids: fileIds,
      instruction,
    }
  }, [routeState])

  const handleAutoStarted = useCallback(() => {
    navigate(location.pathname, { replace: true, state: null })
  }, [navigate, location.pathname])

  // --- 报告数据：doc 通道流式 markdown + 交付后的 report.json ------------------
  const activeDocId = session?.activeDocId ?? null
  const doc = activeDocId ? (session?.docs[activeDocId] ?? null) : null

  /** 已确认的权要树（claim_tree 卡载荷）：报告第三节在装配前先用它渲染。 */
  const claimTree = useMemo(() => {
    const items = session?.items ?? []
    for (let i = items.length - 1; i >= 0; i -= 1) {
      const item = items[i]
      if (item.kind !== 'stage' || item.stage.type !== 'claim_tree') continue
      const payload = item.stage.payload as InteractionRequiredEvent | null
      return normalizeClaimTree(payload?.default ?? null)
    }
    return null
  }, [session?.items])

  const delivered = (session?.versions.length ?? 0) > 0 || (session?.pipeline.done ?? false)
  const reportQuery = useReaderReport(caseId, delivered)
  const bundle = reportQuery.data ?? null

  /** 导出优先级：本轮交付物 → report 接口带回的交付物 → 流式/接口 markdown 兜底。 */
  const handleExportMd = useCallback(() => {
    const hit = doc?.downloads?.find(
      (d) => String(d.kind).endsWith('md') || d.filename.toLowerCase().endsWith('.md'),
    )
    if (hit) {
      downloadUrl(artifactDownloadUrl(hit.artifact_id), hit.filename)
      return
    }
    if (bundle?.artifactId) {
      downloadUrl(
        artifactDownloadUrl(bundle.artifactId),
        bundle.filename ?? zh.reader.panel.exportFilename,
      )
      return
    }
    const markdown = doc?.markdown || bundle?.markdown || ''
    if (markdown) {
      downloadText(
        markdown,
        doc?.filename ?? bundle?.filename ?? zh.reader.panel.exportFilename,
        'text/markdown;charset=utf-8',
      )
      return
    }
    pushToast('info', zh.reader.panel.exportUnavailable)
  }, [bundle, doc, pushToast])

  /** 追问：流水线未跑过时按首轮启动，其余情况以指令启动新一轮。 */
  const handleSend = useCallback(
    async (text: string, fileIds: string[]) => {
      const pubNo = normalizePubNo(text)
      try {
        await startPipeline.mutateAsync({
          pub_no: isLikelyPubNo(pubNo) ? pubNo : '',
          file_id: fileIds[0] ?? null,
          file_ids: fileIds,
          instruction: text,
        })
      } catch (e) {
        const message = e instanceof Error ? e.message : ''
        pushToast('error', message || zh.reader.startFailed)
        useComposerStore.getState().setDraft(caseId, text)
      }
    },
    [caseId, pushToast, startPipeline],
  )

  return (
    <WorkspaceShell
      caseId={caseId}
      stepPresets={READER_STEPS}
      accent="indigo"
      fileAccept={FILE_ACCEPT}
      docTitle={zh.reader.docTitle}
      composerPlaceholder={zh.reader.composerPlaceholder}
      mockScript="reader"
      emptyState={<ReaderIntakeCard caseId={caseId} />}
      autoStartPayload={autoStartPayload}
      onAutoStarted={handleAutoStarted}
      onSend={(text, fileIds) => void handleSend(text, fileIds)}
      documentPanel={
        <ReportPanel
          title={session?.title ?? zh.reader.docTitle}
          markdown={doc?.markdown || (bundle?.markdown ?? '')}
          streaming={doc?.streaming ?? false}
          version={doc?.version ?? bundle?.version ?? undefined}
          report={bundle?.report ?? null}
          claimTree={claimTree}
          unresolved={bundle?.unresolved}
          onExportMd={handleExportMd}
        />
      }
    />
  )
}
