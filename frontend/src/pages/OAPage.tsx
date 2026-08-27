import { useCallback, useMemo, useState } from 'react'
import { useLocation, useNavigate, useParams } from 'react-router-dom'
import { ScaleIcon } from '@heroicons/react/24/outline'
import { zh } from '../i18n/zh'
import { isMockMode } from '../lib/sse'
import { uploadCaseFiles } from '../api/uploads'
import { useStartPipeline } from '../api/pipeline'
import { useComposerStore } from '../stores/composerStore'
import { useUiStore } from '../stores/uiStore'
import { Dropzone } from '../components/upload/Dropzone'
import { Spinner } from '../components/ui/Spinner'
import { WorkspaceShell } from '../components/workspace/WorkspaceShell'
import type { DisplayStepPreset } from '../components/pipeline/StepProgress'

/**
 * §2.8 答复展示步骤集 → 后端 oa 步骤（backend-architecture.md §4 模块④）：
 * notice_extract(门控 oa_issues) → retrieve(门控 oa_retrieve) →
 * strategy(门控 oa_strategy) → draft → review_deliver(门控 confirm)。
 * 首步「上传通知书」为纯展示步骤（无后端 step_key，后续步骤有进展即视为完成）。
 */
const OA_STEPS: DisplayStepPreset[] = [
  { key: 'upload', label: zh.oa.steps.upload },
  { key: 'notice_extract', label: zh.oa.steps.notice_extract },
  { key: 'retrieve', label: zh.oa.steps.retrieve },
  { key: 'strategy', label: zh.oa.steps.strategy },
  { key: 'draft', label: zh.oa.steps.draft },
  { key: 'review_deliver', label: zh.oa.steps.review_deliver },
]

const FILE_ACCEPT = '.pdf,.doc,.docx,.md,.txt'

/** HomePage 跳转时携带的自动启动参数。 */
interface OALocationState {
  autoStart?: boolean
  fileIds?: string[]
  instruction?: string
}

/** §3.5 空会话 mini-hero：上传审查意见通知书 PDF（上传即启动流水线）。 */
function OAMiniHero({ caseId }: { caseId: string }) {
  const pushToast = useUiStore((s) => s.pushToast)
  const startPipeline = useStartPipeline(caseId)
  const [uploading, setUploading] = useState(false)

  const handleFiles = useCallback(
    async (files: File[]) => {
      if (files.length === 0 || uploading) return
      setUploading(true)
      try {
        const uploaded = await uploadCaseFiles(caseId, files)
        pushToast('info', zh.oa.uploadStarted)
        await startPipeline.mutateAsync({
          file_id: uploaded[0]?.id ?? null,
          file_ids: uploaded.map((f) => f.id),
          instruction: '',
        })
      } catch (e) {
        const message = e instanceof Error ? e.message : ''
        pushToast('error', message || zh.oa.uploadFailed)
      } finally {
        setUploading(false)
      }
    },
    [caseId, pushToast, startPipeline, uploading],
  )

  return (
    <div className="pt-10 flex flex-col items-center text-center gap-4">
      <span className="w-14 h-14 rounded-2xl bg-indigo-50 dark:bg-indigo-500/10 flex items-center justify-center">
        <ScaleIcon className="w-7 h-7 text-[#6366f1]" strokeWidth={1.5} />
      </span>
      <div className="space-y-1.5">
        <h2 className="text-xl sm:text-2xl font-bold tracking-tight text-gray-900 dark:text-gray-100">
          {zh.oa.emptyTitle}
        </h2>
        <p className="text-sm text-gray-500 dark:text-gray-400 max-w-md">{zh.oa.emptyDesc}</p>
      </div>
      <Dropzone
        accept={FILE_ACCEPT}
        title={zh.oa.dropHint}
        hint={zh.oa.dropAccept}
        disabled={uploading}
        onFiles={(files) => void handleFiles(files)}
        className="max-w-lg"
      />
      {uploading ? (
        <span className="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
          <Spinner size="sm" />
          {zh.oa.uploading}
        </span>
      ) : (
        <p className="text-xs text-gray-400 dark:text-gray-500">{zh.oa.composerHint}</p>
      )}
    </div>
  )
}

/**
 * §3.5 OAPage（审查意见答复工作台）：WorkspaceShell（indigo accent，6 步）
 * + 空会话上传通知书 mini-hero；三处 HITL 卡片（oa_issues / oa_retrieve /
 * oa_strategy）经 stageCardRegistry 渲染，答复草稿流入右侧 DocumentPanel。
 */
export function OAPage() {
  const { id } = useParams<{ id: string }>()
  const caseId = id ?? ''
  const location = useLocation()
  const navigate = useNavigate()
  const pushToast = useUiStore((s) => s.pushToast)

  const startPipeline = useStartPipeline(caseId)

  const routeState = (location.state ?? null) as OALocationState | null

  const autoStartPayload = useMemo(() => {
    if (!routeState?.autoStart || isMockMode()) return null
    const fileIds = routeState.fileIds ?? []
    return {
      file_id: fileIds[0] ?? null,
      file_ids: fileIds,
      instruction: routeState.instruction ?? '',
    }
  }, [routeState])

  const handleAutoStarted = useCallback(() => {
    navigate(location.pathname, { replace: true, state: null })
  }, [navigate, location.pathname])

  /**
   * chat composer 发送：以补充说明 + 附件启动新一轮答复
   * （POST /pipeline/start，run_group 由后端裁定）。
   */
  const handleSend = useCallback(
    async (text: string, fileIds: string[]) => {
      try {
        await startPipeline.mutateAsync({
          file_id: fileIds[0] ?? null,
          file_ids: fileIds,
          instruction: text,
        })
      } catch (e) {
        const message = e instanceof Error ? e.message : ''
        pushToast('error', message || zh.oa.startFailed)
        useComposerStore.getState().setDraft(caseId, text)
      }
    },
    [caseId, pushToast, startPipeline],
  )

  return (
    <WorkspaceShell
      caseId={caseId}
      stepPresets={OA_STEPS}
      accent="indigo"
      fileAccept={FILE_ACCEPT}
      docTitle={zh.oa.docTitle}
      composerPlaceholder={zh.oa.composerPlaceholder}
      mockScript="oa"
      emptyState={<OAMiniHero caseId={caseId} />}
      autoStartPayload={autoStartPayload}
      onAutoStarted={handleAutoStarted}
      onSend={(text, fileIds) => void handleSend(text, fileIds)}
    />
  )
}
