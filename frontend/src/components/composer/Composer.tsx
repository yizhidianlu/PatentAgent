import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { zh } from '../../i18n/zh'
import { cn } from '../../lib/cn'
import { useUiStore } from '../../stores/uiStore'
import {
  useComposerContext,
  useComposerStore,
  type ComposerAttachment,
} from '../../stores/composerStore'
import { uploadCaseFile } from '../../api/uploads'
import { ComposerInput, type ComposerInputHandle } from './ComposerInput'
import { FilePill } from './FilePill'
import { PlusMenu } from './PlusMenu'
import { SendButton, type SendButtonState } from './SendButton'
import { DragOverlay } from './DragOverlay'

export interface ComposerHandle {
  focus: () => void
  openFilePicker: (kind: 'file' | 'image') => void
}

export interface ComposerProps {
  /** 草稿上下文：'home' 或 sessionId。 */
  contextId: string
  variant?: 'hero' | 'chat'
  accent?: 'indigo' | 'orange'
  /**
   * hero 变体专用：渲染在拖放外壳**内部**、内卡上方的 hero 头部
   * （渐变胶囊 + logo/标题组）。参考站实测外壳同时包裹标题，
   * 因此拖文件到标题区域一样触发上传高亮。
   */
  header?: ReactNode
  /**
   * 内卡底部工具栏最左侧插槽（首页 = ModuleToggle 分段切换器）。
   * sm 以下折行到输入框上方，避免窄屏挤压 +/发送按钮。
   */
  toolbarLeft?: ReactNode
  /** 工具栏右侧、加号菜单之前的插槽（模型档位开关）。 */
  toolbarRight?: ReactNode
  placeholder: string
  onSend: (text: string, attachments: ComposerAttachment[]) => void
  busy?: boolean
  /** busy 态点击发送钮（StopIcon）时回调。 */
  onStop?: () => void
  /** 已有案件：附件添加即上传；缺省（首页）先本地暂存，提交流程再上传。 */
  caseId?: string
  /** PlusMenu「上传文件」的 accept（按模块）。 */
  fileAccept?: string
  /** PlusMenu「上传图片」的 accept（按模块）。 */
  imageAccept?: string
  className?: string
}

/** 上传大小上限（与 zh.composer.fileTooLarge 文案保持一致）。 */
const MAX_FILE_MB = 100

function matchesAccept(file: File, accept: string): boolean {
  const mime = (file.type || '').toLowerCase()
  const name = file.name.toLowerCase()
  return accept
    .split(',')
    .map((t) => t.trim().toLowerCase())
    .filter(Boolean)
    .some((token) => {
      if (token.startsWith('.')) return name.endsWith(token)
      if (token.endsWith('/*')) return mime.startsWith(token.slice(0, -1))
      return mime === token
    })
}

/**
 * §2.3 Composer（招牌组件）：hero/chat 双变体、indigo/orange accent、
 * rounded-3xl 外壳、聚焦光晕内卡、window 级拖放、FilePill 网格、
 * contentEditable 输入、PlusMenu、SendButton 三态。
 */
export const Composer = forwardRef<ComposerHandle, ComposerProps>(function Composer(
  {
    contextId,
    variant = 'hero',
    accent = 'indigo',
    header,
    toolbarLeft,
    toolbarRight,
    placeholder,
    onSend,
    busy = false,
    onStop,
    caseId,
    fileAccept,
    imageAccept,
    className,
  },
  ref,
) {
  const pushToast = useUiStore((s) => s.pushToast)
  const { draft, attachments } = useComposerContext(contextId)
  const setDraft = useComposerStore((s) => s.setDraft)
  const removeAttachment = useComposerStore((s) => s.removeAttachment)

  const [focused, setFocused] = useState(false)
  const [dragActive, setDragActive] = useState(false)
  const inputRef = useRef<ComposerInputHandle>(null)
  const filePickerRef = useRef<HTMLInputElement>(null)
  const imagePickerRef = useRef<HTMLInputElement>(null)

  useImperativeHandle(ref, () => ({
    focus: () => inputRef.current?.focus(),
    openFilePicker: (kind) =>
      (kind === 'image' ? imagePickerRef : filePickerRef).current?.click(),
  }))

  /** 已有案件时的即时上传（含错误重试）。 */
  const startUpload = useCallback(
    async (att: ComposerAttachment) => {
      if (!caseId) return
      const { updateAttachment } = useComposerStore.getState()
      updateAttachment(contextId, att.localId, {
        status: 'uploading',
        progress: 0,
        error: undefined,
      })
      try {
        const uploaded = await uploadCaseFile(caseId, att.file, {
          onProgress: (percent) =>
            useComposerStore
              .getState()
              .updateAttachment(contextId, att.localId, { progress: percent }),
        })
        useComposerStore.getState().updateAttachment(contextId, att.localId, {
          status: 'done',
          progress: 100,
          fileId: uploaded.id,
        })
      } catch (e) {
        const message = e instanceof Error ? e.message : zh.composer.uploadError
        useComposerStore.getState().updateAttachment(contextId, att.localId, {
          status: 'error',
          error: message,
        })
        pushToast('error', `${zh.composer.uploadError}：${att.name}`)
      }
    },
    [caseId, contextId, pushToast],
  )

  const addFiles = useCallback(
    (list: FileList | File[]) => {
      const files = Array.from(list)
      if (files.length === 0) return
      const accepted: File[] = []
      for (const file of files) {
        if (file.size > MAX_FILE_MB * 1024 * 1024) {
          pushToast('warning', `${zh.composer.fileTooLarge}：${file.name}`)
          continue
        }
        const acceptOk =
          (!fileAccept && !imageAccept) ||
          (fileAccept ? matchesAccept(file, fileAccept) : false) ||
          (imageAccept ? matchesAccept(file, imageAccept) : false)
        if (!acceptOk) {
          pushToast('warning', `${zh.composer.fileTypeRejected}：${file.name}`)
          continue
        }
        accepted.push(file)
      }
      if (accepted.length === 0) return
      const created = useComposerStore
        .getState()
        .addFiles(contextId, accepted, caseId ? 'uploading' : 'done')
      if (caseId) created.forEach((att) => void startUpload(att))
    },
    [caseId, contextId, fileAccept, imageAccept, pushToast, startUpload],
  )

  const retryAttachment = useCallback(
    (att: ComposerAttachment) => {
      if (caseId) void startUpload(att)
      else
        useComposerStore
          .getState()
          .updateAttachment(contextId, att.localId, { status: 'done', error: undefined })
    },
    [caseId, contextId, startUpload],
  )

  // window 级 dragenter 计数（§2.3 拖放态）
  useEffect(() => {
    let depth = 0
    const isFileDrag = (e: DragEvent) =>
      !!e.dataTransfer && Array.from(e.dataTransfer.types).includes('Files')
    const onDragEnter = (e: DragEvent) => {
      if (!isFileDrag(e)) return
      depth += 1
      setDragActive(true)
    }
    const onDragLeave = (e: DragEvent) => {
      if (!isFileDrag(e)) return
      depth = Math.max(0, depth - 1)
      if (depth === 0) setDragActive(false)
    }
    const onDragOver = (e: DragEvent) => {
      if (isFileDrag(e)) e.preventDefault()
    }
    const onDrop = (e: DragEvent) => {
      if (isFileDrag(e)) e.preventDefault()
      depth = 0
      setDragActive(false)
    }
    window.addEventListener('dragenter', onDragEnter)
    window.addEventListener('dragleave', onDragLeave)
    window.addEventListener('dragover', onDragOver)
    window.addEventListener('drop', onDrop)
    return () => {
      window.removeEventListener('dragenter', onDragEnter)
      window.removeEventListener('dragleave', onDragLeave)
      window.removeEventListener('dragover', onDragOver)
      window.removeEventListener('drop', onDrop)
    }
  }, [])

  const text = draft.trim()
  const hasUploading = attachments.some((a) => a.status === 'uploading')
  const canSend = !busy && !hasUploading && (text.length > 0 || attachments.length > 0)
  const sendState: SendButtonState = busy ? 'busy' : canSend ? 'ready' : 'disabled'

  const handleSend = () => {
    if (busy) {
      onStop?.()
      return
    }
    if (!canSend) return
    onSend(text, attachments)
  }

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault()
    if (e.dataTransfer.files.length > 0) addFiles(e.dataTransfer.files)
  }

  const plusMenu = (
    <PlusMenu
      onPickFile={() => filePickerRef.current?.click()}
      onPickImage={() => imagePickerRef.current?.click()}
    />
  )
  const sendButton = <SendButton state={sendState} accent={accent} onClick={handleSend} />

  const innerCard = (
    <div
      className={cn(
        // 参考站实测：idle 1px 描边，聚焦才 2px + 光晕；
        // 聚焦时 p-4→p-[15px] 抵消 1px 描边增量，box-sizing:border-box 下内容零跳动。
        'w-full rounded-2xl bg-white dark:bg-gray-800 shadow-sm relative transition-all duration-300',
        !focused && 'p-4 border border-gray-200 dark:border-gray-700',
        focused &&
          'p-[15px] border-2 ' +
            (accent === 'orange'
              ? 'border-orange-500 dark:bg-gray-900 shadow-[0_0_12px_rgba(249,115,22,0.15)]'
              : 'border-[#6366f1] dark:bg-gray-900 shadow-[0_0_12px_rgba(99,102,241,0.15)]'),
      )}
    >
      {attachments.length > 0 && (
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 mb-3">
          {attachments.map((att) => (
            <FilePill
              key={att.localId}
              attachment={att}
              onRemove={() => removeAttachment(contextId, att.localId)}
              onRetry={() => retryAttachment(att)}
            />
          ))}
        </div>
      )}

      {/* 窄屏备用行：分段器折到输入框上方（参考站 flex sm:hidden 同思路） */}
      {toolbarLeft && (
        <div className="flex sm:hidden items-center mb-3 -mx-1 px-1 overflow-x-auto scrollbar-thin">
          {toolbarLeft}
        </div>
      )}

      <ComposerInput
        ref={inputRef}
        value={draft}
        onChange={(v) => setDraft(contextId, v)}
        onSubmit={handleSend}
        placeholder={placeholder}
        onFocusChange={setFocused}
      />

      <div className="flex items-center justify-between gap-2 mt-4">
        {toolbarLeft ? (
          <>
            <div className="hidden sm:flex items-center min-w-0">{toolbarLeft}</div>
            <div className="flex items-center gap-2 ml-auto shrink-0">
              {toolbarRight}
              {plusMenu}
              {sendButton}
            </div>
          </>
        ) : (
          <>
            {toolbarRight}
            <div className="flex items-center gap-2 ml-auto shrink-0">
              {plusMenu}
              {sendButton}
            </div>
          </>
        )}
      </div>
    </div>
  )

  const hiddenInputs = (
    <>
      <input
        ref={filePickerRef}
        type="file"
        multiple
        accept={fileAccept}
        className="hidden"
        onChange={(e) => {
          if (e.target.files) addFiles(e.target.files)
          e.target.value = ''
        }}
      />
      <input
        ref={imagePickerRef}
        type="file"
        multiple
        accept={imageAccept}
        className="hidden"
        onChange={(e) => {
          if (e.target.files) addFiles(e.target.files)
          e.target.value = ''
        }}
      />
    </>
  )

  if (variant === 'chat') {
    return (
      <div className={cn('sticky bottom-0 w-full', className)}>
        <div className="max-w-[800px] mx-auto px-4 pb-3">
          <div
            className={cn(
              'relative rounded-2xl border-2 border-transparent transition-colors',
              dragActive &&
                'border-dashed border-blue-500 bg-blue-50/50 dark:bg-blue-500/10 ring-4 ring-blue-500/10',
            )}
            onDrop={handleDrop}
            onDragOver={(e) => e.preventDefault()}
          >
            <DragOverlay visible={dragActive} />
            {innerCard}
            {hiddenInputs}
          </div>
          <p className="text-center text-xs text-gray-400 mt-2">{zh.common.aiDisclaimer}</p>
        </div>
      </div>
    )
  }

  // hero 外壳（参考站实测）：同时包裹 hero 头部与内卡，
  // 因此拖文件到标题区域也会触发上传高亮。
  return (
    <div
      className={cn(
        'relative w-full max-w-[800px] mx-auto flex flex-col items-center gap-3 sm:gap-4 mt-0',
        'rounded-3xl p-3 sm:p-4 border-2 border-transparent transition-all duration-200',
        dragActive &&
          'border-dashed border-blue-500 bg-blue-50/50 dark:bg-blue-500/10 ring-4 ring-blue-500/10',
        className,
      )}
      onDrop={handleDrop}
      onDragOver={(e) => e.preventDefault()}
    >
      {hiddenInputs}
      <DragOverlay visible={dragActive} className="rounded-3xl" />
      {header}
      <div className="w-full">{innerCard}</div>
    </div>
  )
})
