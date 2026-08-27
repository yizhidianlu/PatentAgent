import { useCallback, useMemo, useState } from 'react'
import { useLocation, useNavigate, useParams } from 'react-router-dom'
import { DocumentTextIcon } from '@heroicons/react/24/outline'
import { zh } from '../i18n/zh'
import { isMockMode } from '../lib/sse'
import { uploadCaseFiles } from '../api/uploads'
import { useStartPipeline } from '../api/pipeline'
import { isIterateUnavailable, useDisclosureIterate } from '../api/disclosure'
import { useCaseSession } from '../stores/sessionStore'
import { useComposerStore } from '../stores/composerStore'
import { useUiStore } from '../stores/uiStore'
import { Dropzone } from '../components/upload/Dropzone'
import { Spinner } from '../components/ui/Spinner'
import { WorkspaceShell } from '../components/workspace/WorkspaceShell'
import {
  computeDynamicSteps,
  type DisplayStepPreset,
} from '../components/pipeline/StepProgress'
import type { PipelineStepState } from '../stores/sessionStore'

/**
 * §2.8 交底书展示步骤集 → 后端 disclosure 步骤（backend-architecture.md §4 模块①）：
 * intake → material_scan → points_mining → prior_art_search → preview → build →
 * self_check → deliver。实用新型 / 外观设计分支会多出 schema_fill 等步骤，
 * 故步骤条经 computeDynamicSteps 以后端 pipeline.steps 为准渲染，本表仅作骨架与名称美化。
 */
const DISCLOSURE_STEPS: DisplayStepPreset[] = [
  { key: 'intake', label: zh.disclosure.steps.intake },
  { key: 'material_scan', label: zh.disclosure.steps.material_scan },
  { key: 'points_mining', label: zh.disclosure.steps.points_mining },
  { key: 'prior_art_search', label: zh.disclosure.steps.prior_art_search },
  { key: 'preview', label: zh.disclosure.steps.preview },
  { key: 'build', label: zh.disclosure.steps.build },
  { key: 'self_check', label: zh.disclosure.steps.self_check },
  { key: 'deliver', label: zh.disclosure.steps.deliver },
]

const FILE_ACCEPT = '.pdf,.doc,.docx,.ppt,.pptx,.md,.txt,.png,.jpg,.jpeg,.webp'

/** HomePage 跳转时携带的自动启动参数。 */
interface DisclosureLocationState {
  autoStart?: boolean
  fileIds?: string[]
  instruction?: string
}

/** §3.2 空会话 mini-hero：上传项目材料（上传即启动流水线）或直接在下方描述技术方案。 */
function DisclosureMiniHero({ caseId }: { caseId: string }) {
  const pushToast = useUiStore((s) => s.pushToast)
  const startPipeline = useStartPipeline(caseId)
  const [uploading, setUploading] = useState(false)

  const handleFiles = useCallback(
    async (files: File[]) => {
      if (files.length === 0 || uploading) return
      setUploading(true)
      try {
        const uploaded = await uploadCaseFiles(caseId, files)
        pushToast('info', zh.disclosure.uploadStarted)
        await startPipeline.mutateAsync({
          file_ids: uploaded.map((f) => f.id),
          instruction: '',
        })
      } catch (e) {
        const message = e instanceof Error ? e.message : ''
        pushToast('error', message || zh.disclosure.uploadFailed)
      } finally {
        setUploading(false)
      }
    },
    [caseId, pushToast, startPipeline, uploading],
  )

  return (
    <div className="pt-10 flex flex-col items-center text-center gap-4">
      <span className="w-14 h-14 rounded-2xl bg-indigo-50 dark:bg-indigo-500/10 flex items-center justify-center">
        <DocumentTextIcon className="w-7 h-7 text-[#6366f1]" strokeWidth={1.5} />
      </span>
      <div className="space-y-1.5">
        <h2 className="text-xl sm:text-2xl font-bold tracking-tight text-gray-900 dark:text-gray-100">
          {zh.disclosure.emptyTitle}
        </h2>
        <p className="text-sm text-gray-500 dark:text-gray-400 max-w-md">
          {zh.disclosure.emptyDesc}
        </p>
      </div>
      <Dropzone
        accept={FILE_ACCEPT}
        title={zh.disclosure.dropHint}
        hint={zh.disclosure.dropAccept}
        disabled={uploading}
        onFiles={(files) => void handleFiles(files)}
        className="max-w-lg"
      />
      {uploading ? (
        <span className="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
          <Spinner size="sm" />
          {zh.disclosure.uploading}
        </span>
      ) : (
        <p className="text-xs text-gray-400 dark:text-gray-500">{zh.disclosure.composerHint}</p>
      )}
    </div>
  )
}

/**
 * §3.2 DisclosurePage（旗舰工作台）：WorkspaceShell（indigo accent，8 步动态步骤条）
 * + 空会话 mini-hero；chat composer 发送走 POST /cases/{id}/disclosure/iterate
 * （首轮未启动时退化为 POST /pipeline/start）。
 */
export function DisclosurePage() {
  const { id } = useParams<{ id: string }>()
  const caseId = id ?? ''
  const location = useLocation()
  const navigate = useNavigate()
  const pushToast = useUiStore((s) => s.pushToast)

  const session = useCaseSession(caseId)
  const startPipeline = useStartPipeline(caseId)
  const iterate = useDisclosureIterate(caseId)

  const routeState = (location.state ?? null) as DisclosureLocationState | null

  const autoStartPayload = useMemo(() => {
    if (!routeState?.autoStart || isMockMode()) return null
    const fileIds = routeState.fileIds ?? []
    return {
      file_ids: fileIds,
      instruction: routeState.instruction ?? '',
    }
  }, [routeState])

  const handleAutoStarted = useCallback(() => {
    // 清掉路由 state，避免刷新 / 返回时重复启动
    navigate(location.pathname, { replace: true, state: null })
  }, [navigate, location.pathname])

  // 步骤条以后端 pipeline.steps 为准（分支步骤动态增减）
  const stepResolver = useCallback(
    (steps: PipelineStepState[]) =>
      computeDynamicSteps(DISCLOSURE_STEPS, steps, zh.disclosure.steps),
    [],
  )

  /** 已跑过流水线（有任一步骤记录）→ 走迭代；否则首轮启动。 */
  const started = (session?.pipeline.steps.length ?? 0) > 0

  /**
   * mock 模式剧本：缺省回放发明全流程；案件 id 含 utility / design / schema 时
   * 交给 mockServer.pickScript 命中 A3b「填表与线稿」分支剧本（schema_fill 卡片 QA）。
   */
  const mockScript = /utility|design|schema/i.test(caseId) ? undefined : ('disclosure' as const)

  const handleSend = useCallback(
    async (text: string, fileIds: string[]) => {
      if (!started) {
        try {
          await startPipeline.mutateAsync({ instruction: text, file_ids: fileIds })
        } catch (e) {
          const message = e instanceof Error ? e.message : ''
          pushToast('error', message || zh.workspace.startFailed)
          useComposerStore.getState().setDraft(caseId, text)
        }
        return
      }
      try {
        await iterate.mutateAsync({ mode: 'auto', instruction: text, file_ids: fileIds })
      } catch (e) {
        // 后端迭代端点未就绪：提示并把输入还回 composer
        if (isIterateUnavailable(e)) {
          pushToast('warning', zh.disclosure.iterateUnavailable)
        } else {
          const message = e instanceof Error ? e.message : ''
          pushToast('error', message || zh.disclosure.iterateFailed)
        }
        useComposerStore.getState().setDraft(caseId, text)
      }
    },
    [caseId, iterate, pushToast, startPipeline, started],
  )

  return (
    <WorkspaceShell
      caseId={caseId}
      stepPresets={DISCLOSURE_STEPS}
      stepResolver={stepResolver}
      accent="indigo"
      fileAccept={FILE_ACCEPT}
      docTitle={zh.disclosure.docTitle}
      composerPlaceholder={zh.disclosure.composerPlaceholder}
      mockScript={mockScript}
      emptyState={<DisclosureMiniHero caseId={caseId} />}
      autoStartPayload={autoStartPayload}
      onAutoStarted={handleAutoStarted}
      onSend={(text, fileIds) => void handleSend(text, fileIds)}
    />
  )
}
