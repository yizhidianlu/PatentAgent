import { useCallback, useMemo, useState } from 'react'
import { useLocation, useNavigate, useParams } from 'react-router-dom'
import { DocumentArrowUpIcon } from '@heroicons/react/24/outline'
import { zh } from '../i18n/zh'
import { isMockMode } from '../lib/sse'
import { uploadCaseFile } from '../api/uploads'
import { useStartPipeline } from '../api/pipeline'
import { useUiStore } from '../stores/uiStore'
import { Dropzone } from '../components/upload/Dropzone'
import { Spinner } from '../components/ui/Spinner'
import { WorkspaceShell } from '../components/workspace/WorkspaceShell'
import type { DisplayStepPreset } from '../components/pipeline/StepProgress'

/**
 * §2.8 论文转专利展示步骤集 → 后端 p2p 步骤（backend-architecture.md §4 模块②）：
 * input_check → extraction → draft → rules_check → content_review → drawings → build。
 * 「上传论文」发生在流水线启动之前，故不绑定后端步骤，由后续步骤的进展隐含置 done。
 */
const PAPER_STEPS: DisplayStepPreset[] = [
  { key: 'upload', label: zh.paper.steps.upload, matchKeys: ['upload_paper'] },
  { key: 'mode', label: zh.paper.steps.mode, matchKeys: ['input_check'] },
  {
    key: 'parts',
    label: zh.paper.steps.parts,
    matchKeys: ['extraction', 'draft', 'rules_check', 'content_review'],
  },
  { key: 'figures', label: zh.paper.steps.figures, matchKeys: ['drawings'] },
  { key: 'delivery', label: zh.paper.steps.delivery, matchKeys: ['build'] },
]

/** HomePage 跳转时携带的自动启动参数。 */
interface PaperLocationState {
  autoStart?: boolean
  fileIds?: string[]
  mode?: string
  instruction?: string
}

/** §3.3 空会话 mini-hero：引导上传论文 PDF，上传即启动流水线。 */
function PaperMiniHero({ caseId }: { caseId: string }) {
  const pushToast = useUiStore((s) => s.pushToast)
  const startPipeline = useStartPipeline(caseId)
  const [uploading, setUploading] = useState(false)

  const handleFiles = useCallback(
    async (files: File[]) => {
      const file = files[0]
      if (!file || uploading) return
      setUploading(true)
      try {
        const uploaded = await uploadCaseFile(caseId, file)
        pushToast('info', zh.paper.uploadStarted)
        await startPipeline.mutateAsync({
          file_id: uploaded.id,
          file_ids: [uploaded.id],
          mode: 'direct',
        })
      } catch (e) {
        const message = e instanceof Error ? e.message : ''
        pushToast('error', message || zh.paper.uploadFailed)
      } finally {
        setUploading(false)
      }
    },
    [caseId, pushToast, startPipeline, uploading],
  )

  return (
    <div className="pt-10 flex flex-col items-center text-center gap-4">
      <span className="w-14 h-14 rounded-2xl bg-orange-50 dark:bg-orange-500/10 flex items-center justify-center">
        <DocumentArrowUpIcon className="w-7 h-7 text-orange-500" strokeWidth={1.5} />
      </span>
      <div className="space-y-1.5">
        <h2 className="text-xl sm:text-2xl font-bold tracking-tight text-gray-900 dark:text-gray-100">
          {zh.paper.emptyTitle}
        </h2>
        <p className="text-sm text-gray-500 dark:text-gray-400 max-w-md">{zh.paper.emptyDesc}</p>
      </div>
      <Dropzone
        accept=".pdf,application/pdf"
        multiple={false}
        title={zh.paper.dropHint}
        hint={zh.paper.dropAccept}
        disabled={uploading}
        onFiles={(files) => void handleFiles(files)}
        className="max-w-lg"
      />
      {uploading && (
        <span className="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
          <Spinner size="sm" />
          {zh.paper.uploading}
        </span>
      )}
    </div>
  )
}

/**
 * §3.3 PaperPage：WorkspaceShell（橙 accent，5 步）+ 空会话 mini-hero；
 * 由 HomePage 携 file_id / mode 跳转进入时自动 POST /pipeline/start。
 */
export function PaperPage() {
  const { id } = useParams<{ id: string }>()
  const caseId = id ?? ''
  const location = useLocation()
  const navigate = useNavigate()

  const routeState = (location.state ?? null) as PaperLocationState | null

  const autoStartPayload = useMemo(() => {
    if (!routeState?.autoStart || isMockMode()) return null
    const fileIds = routeState.fileIds ?? []
    return {
      file_id: fileIds[0] ?? null,
      file_ids: fileIds,
      mode: routeState.mode ?? 'direct',
      instruction: routeState.instruction ?? '',
    }
  }, [routeState])

  const handleAutoStarted = useCallback(() => {
    // 清掉路由 state，避免刷新 / 返回时重复启动
    navigate(location.pathname, { replace: true, state: null })
  }, [navigate, location.pathname])

  return (
    <WorkspaceShell
      caseId={caseId}
      stepPresets={PAPER_STEPS}
      accent="orange"
      fileAccept=".pdf,application/pdf"
      docTitle={zh.paper.docTitle}
      composerPlaceholder={zh.paper.composerPlaceholder}
      mockScript="paper"
      emptyState={<PaperMiniHero caseId={caseId} />}
      autoStartPayload={autoStartPayload}
      onAutoStarted={handleAutoStarted}
    />
  )
}
