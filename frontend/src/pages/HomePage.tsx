import { useCallback, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { zh } from '../i18n/zh'
import { useUiStore } from '../stores/uiStore'
import { useComposerStore, type ComposerAttachment } from '../stores/composerStore'
import { useCreateCase } from '../api/sessions'
import { uploadCaseFile } from '../api/uploads'
import { Composer, type ComposerHandle } from '../components/composer/Composer'
import { ActivityPill } from '../components/home/ActivityPill'
import { HeroGreeting } from '../components/home/HeroGreeting'
import { ModuleToggle } from '../components/home/ModuleToggle'
import { TierToggle } from '../components/composer/TierToggle'
import { useModelTiers } from '../api/settings'
import type { ModelTier } from '../api/sessions'
import { FeatureChips, type ChipActionEvent } from '../components/home/FeatureChips'
import {
  MODULE_META,
  readStoredModule,
  storeModule,
  type HomeModule,
} from '../components/home/moduleConfig'

/** 首页 Composer 的草稿上下文。 */
const HOME_CONTEXT = 'home'

/** 案件标题取文本前 20 字。 */
const TITLE_MAX_CHARS = 20

/** 论文转专利缺省模式（§3.3：direct = 直接生成）。 */
const DEFAULT_PAPER_MODE = 'direct'

/**
 * §3.1 HomePage（参考站 Playwright 实测结构）：
 * `p-4 pb-20 flex flex-col flex-1 pt-[10vh]` →
 * 拖放外壳（Composer hero）内含 [ActivityPill + HeroGreeting] + 内卡
 * （内卡工具栏左侧 = ModuleToggle）→ 外壳之外 FeatureChips。
 * 提交：POST /cases {module, title:前20字} → 上传附件 → navigate 工作台。
 */
export function HomePage() {
  const navigate = useNavigate()
  const pushToast = useUiStore((s) => s.pushToast)
  const createCase = useCreateCase()

  const [module, setModule] = useState<HomeModule>(readStoredModule)
  // 档位默认跟随设置里的默认档；用户在首页改过之后，本次会话内保持他的选择
  const tiersQuery = useModelTiers()
  const [tierOverride, setTierOverride] = useState<ModelTier | null>(null)
  const tier: ModelTier = tierOverride ?? tiersQuery.data?.default_tier ?? 'deep'
  const setTier = useCallback((next: ModelTier) => setTierOverride(next), [])
  const [submitting, setSubmitting] = useState(false)
  const composerRef = useRef<ComposerHandle>(null)
  // 上传失败重试时复用已建案件，避免重复建案
  const pendingCaseIdRef = useRef<string | null>(null)

  const meta = MODULE_META[module]

  const changeModule = useCallback((next: HomeModule) => {
    storeModule(next)
    setModule(next)
  }, [])

  const handleChipAction = useCallback((event: ChipActionEvent) => {
    if (event.type === 'prefill') {
      useComposerStore.getState().setDraft(HOME_CONTEXT, event.text)
      composerRef.current?.focus()
    } else {
      composerRef.current?.openFilePicker(event.kind)
    }
  }, [])

  const handleSend = useCallback(
    async (text: string, attachments: ComposerAttachment[]) => {
      if (submitting) return
      setSubmitting(true)
      const store = useComposerStore.getState()
      try {
        // 1) 建案（重试时复用）
        let caseId = pendingCaseIdRef.current
        if (!caseId) {
          const created = await createCase.mutateAsync({
            module: meta.backendModule,
            title: text ? text.slice(0, TITLE_MAX_CHARS) : undefined,
            // 建案时就带上档位：省掉一次 PATCH，也少一个「PATCH 失败但案件已建」的中间态
            model_tier: tier,
          })
          caseId = created.id
          pendingCaseIdRef.current = caseId
        }

        // 2) 逐个上传本地暂存附件（已有 fileId 的跳过）
        const fileIds: string[] = []
        for (const att of attachments) {
          if (att.fileId) {
            fileIds.push(att.fileId)
            continue
          }
          store.updateAttachment(HOME_CONTEXT, att.localId, {
            status: 'uploading',
            progress: 0,
            error: undefined,
          })
          try {
            const uploaded = await uploadCaseFile(caseId, att.file, {
              onProgress: (percent) =>
                useComposerStore
                  .getState()
                  .updateAttachment(HOME_CONTEXT, att.localId, { progress: percent }),
            })
            store.updateAttachment(HOME_CONTEXT, att.localId, {
              status: 'done',
              progress: 100,
              fileId: uploaded.id,
            })
            fileIds.push(uploaded.id)
          } catch (e) {
            const message = e instanceof Error ? e.message : zh.composer.uploadError
            store.updateAttachment(HOME_CONTEXT, att.localId, {
              status: 'error',
              error: message,
            })
            pushToast('error', `${zh.composer.uploadError}：${att.name}`)
            return // 留在首页，附件可重试后再次发送
          }
        }

        // 3) 进入工作台（携带自动启动流水线所需的 file_ids / mode / 首轮指令）
        store.clearContext(HOME_CONTEXT)
        pendingCaseIdRef.current = null
        navigate(`${meta.routeBase}/${caseId}`, {
          state: {
            autoStart: true,
            fileIds,
            mode: DEFAULT_PAPER_MODE,
            instruction: text,
          },
        })
      } catch (e) {
        const message = e instanceof Error ? e.message : zh.home.errors.createFailed
        pushToast('error', message || zh.home.errors.createFailed)
      } finally {
        setSubmitting(false)
      }
    },
    [createCase, meta, navigate, pushToast, submitting, tier],
  )

  // 参考站实测：胶囊 + logo/标题组同在拖放外壳内部（Composer header 插槽）
  const heroHeader = (
    <div className="flex flex-col items-center gap-1 mb-1 sm:mb-2">
      <div className="flex justify-center w-full mb-1 sm:mb-2">
        <ActivityPill onClick={() => changeModule('paper')} />
      </div>
      <HeroGreeting module={module} />
    </div>
  )

  return (
    // 参考站实测：pt-[10vh] 顶部定位，而非垂直居中
    <div className="p-4 pb-20 flex flex-col flex-1 pt-[10vh]">
      <Composer
        ref={composerRef}
        contextId={HOME_CONTEXT}
        variant="hero"
        accent={meta.accent}
        header={heroHeader}
        toolbarLeft={<ModuleToggle value={module} onChange={changeModule} className="shrink-0" />}
        toolbarRight={<TierToggle value={tier} onChange={setTier} />}
        placeholder={zh.home.placeholders[module]}
        onSend={(text, attachments) => void handleSend(text, attachments)}
        busy={submitting}
        fileAccept={meta.fileAccept}
        imageAccept={meta.imageAccept}
      />
      <div className="w-full max-w-[800px] mx-auto mt-3 sm:mt-4">
        <FeatureChips module={module} onAction={handleChipAction} />
      </div>
    </div>
  )
}
