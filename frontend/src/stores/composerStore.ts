import { create } from 'zustand'

/**
 * Composer 草稿与附件 store（frontend-design.md §5）：
 * per-context（'home' | sessionId）草稿文本 + 附件进度数组。
 */

export type ComposerContextId = string

export type AttachmentStatus = 'uploading' | 'done' | 'error'

export type AttachmentKind = 'pdf' | 'word' | 'ppt' | 'image' | 'markdown' | 'text' | 'other'

export interface ComposerAttachment {
  /** 前端本地 id（上传前就存在）。 */
  localId: string
  file: File
  name: string
  size: number
  kind: AttachmentKind
  /** 0–100。 */
  progress: number
  /**
   * uploading = XHR 上传中；error = 上传失败（可重试）；
   * done + fileId = 已上传；done 无 fileId = 本地暂存（首页未建案时，提交才上传）。
   */
  status: AttachmentStatus
  /** 后端 files.id（上传成功后回填）。 */
  fileId?: string
  /** 上传失败原因。 */
  error?: string
}

export interface ComposerContextState {
  draft: string
  attachments: ComposerAttachment[]
}

const EMPTY_CONTEXT: ComposerContextState = { draft: '', attachments: [] }

let localSeq = 0

function nextLocalId(): string {
  localSeq += 1
  return `att-${Date.now()}-${localSeq}`
}

/** 按 MIME / 扩展名推断附件类型（FilePill 图标用）。 */
export function detectAttachmentKind(file: File): AttachmentKind {
  const mime = (file.type || '').toLowerCase()
  const name = file.name.toLowerCase()
  const dot = name.lastIndexOf('.')
  const ext = dot >= 0 ? name.slice(dot + 1) : ''
  if (mime.startsWith('image/')) return 'image'
  if (mime === 'application/pdf' || ext === 'pdf') return 'pdf'
  if (ext === 'doc' || ext === 'docx' || mime.includes('msword') || mime.includes('wordprocessingml'))
    return 'word'
  if (ext === 'ppt' || ext === 'pptx' || mime.includes('presentation')) return 'ppt'
  if (ext === 'md' || ext === 'markdown') return 'markdown'
  if (ext === 'txt' || mime.startsWith('text/')) return 'text'
  return 'other'
}

export interface ComposerState {
  contexts: Record<ComposerContextId, ComposerContextState>

  setDraft: (contextId: ComposerContextId, draft: string) => void
  /** 添加文件并返回新建的附件记录（含 localId，便于调用方跟踪上传）。 */
  addFiles: (
    contextId: ComposerContextId,
    files: File[],
    status?: AttachmentStatus,
  ) => ComposerAttachment[]
  updateAttachment: (
    contextId: ComposerContextId,
    localId: string,
    patch: Partial<Omit<ComposerAttachment, 'localId' | 'file'>>,
  ) => void
  removeAttachment: (contextId: ComposerContextId, localId: string) => void
  clearContext: (contextId: ComposerContextId) => void
}

export const useComposerStore = create<ComposerState>((set) => ({
  contexts: {},

  setDraft: (contextId, draft) =>
    set((s) => {
      const ctx = s.contexts[contextId] ?? EMPTY_CONTEXT
      return { contexts: { ...s.contexts, [contextId]: { ...ctx, draft } } }
    }),

  addFiles: (contextId, files, status = 'done') => {
    const created: ComposerAttachment[] = files.map((file) => ({
      localId: nextLocalId(),
      file,
      name: file.name,
      size: file.size,
      kind: detectAttachmentKind(file),
      progress: 0,
      status,
    }))
    set((s) => {
      const ctx = s.contexts[contextId] ?? EMPTY_CONTEXT
      return {
        contexts: {
          ...s.contexts,
          [contextId]: { ...ctx, attachments: [...ctx.attachments, ...created] },
        },
      }
    })
    return created
  },

  updateAttachment: (contextId, localId, patch) =>
    set((s) => {
      const ctx = s.contexts[contextId]
      if (!ctx) return s
      return {
        contexts: {
          ...s.contexts,
          [contextId]: {
            ...ctx,
            attachments: ctx.attachments.map((a) =>
              a.localId === localId ? { ...a, ...patch } : a,
            ),
          },
        },
      }
    }),

  removeAttachment: (contextId, localId) =>
    set((s) => {
      const ctx = s.contexts[contextId]
      if (!ctx) return s
      return {
        contexts: {
          ...s.contexts,
          [contextId]: {
            ...ctx,
            attachments: ctx.attachments.filter((a) => a.localId !== localId),
          },
        },
      }
    }),

  clearContext: (contextId) =>
    set((s) => {
      if (!(contextId in s.contexts)) return s
      const contexts = { ...s.contexts }
      delete contexts[contextId]
      return { contexts }
    }),
}))

/** 读取某上下文的草稿与附件（未初始化时返回稳定的空态引用）。 */
export function useComposerContext(contextId: ComposerContextId): ComposerContextState {
  const ctx = useComposerStore((s) => s.contexts[contextId])
  return ctx ?? EMPTY_CONTEXT
}
