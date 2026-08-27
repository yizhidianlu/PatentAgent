import {
  API_BASE,
  ApiError,
  CSRF_HEADER,
  notifyApiAuthFailure,
  readCsrfToken,
} from '../lib/api'
import { isMockMode } from '../lib/sse'
import { zh } from '../i18n/zh'

/**
 * 文件上传（frontend-design.md §5）：XHR 取 upload.onprogress，
 * POST /api/v1/cases/{id}/files multipart（字段名 files，支持多文件）。
 */

/** 后端 files.kind 枚举（models/file.py）。 */
export type FileKind = 'upload' | 'converted_md' | 'extracted_figure' | 'manual'

/** 后端 FileOut（models/file.py）。 */
export interface UploadedFile {
  id: string
  case_id: string
  kind: FileKind
  orig_name: string
  mime: string | null
  size: number | null
  stored_path: string
  md_path: string | null
  meta: Record<string, unknown> | null
  created_at: string
}

export interface UploadOptions {
  /** 0–100 整数百分比。 */
  onProgress?: (percent: number) => void
  signal?: AbortSignal
}

/** 与 lib/api.extractErrorMessage 同规整逻辑的精简版（XHR 场景）。 */
function extractMessage(status: number, payload: unknown): string {
  if (payload && typeof payload === 'object') {
    const obj = payload as Record<string, unknown>
    const detail = obj.detail ?? obj.message ?? obj.error
    if (typeof detail === 'string' && detail) return detail
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0] as Record<string, unknown> | string
      if (typeof first === 'string') return first
      if (first && typeof first.msg === 'string') return first.msg
    }
  }
  if (typeof payload === 'string' && payload) return payload
  return zh.common.httpFailed(status)
}

/** 规整响应体：兼容 FileOut[] / {items:[…]} / 单对象三种形态。 */
function normalizePayload(payload: unknown): UploadedFile[] {
  if (Array.isArray(payload)) return payload as UploadedFile[]
  if (payload && typeof payload === 'object') {
    const items = (payload as { items?: unknown }).items
    if (Array.isArray(items)) return items as UploadedFile[]
    return [payload as UploadedFile]
  }
  return []
}

let mockFileSeq = 0

/** Mock 模式上传（XHR 无法被 fetch 拦截，故在此分流）：模拟进度后返回假 FileOut。 */
function mockUpload(caseId: string, files: File[], options: UploadOptions): Promise<UploadedFile[]> {
  const { onProgress } = options
  return new Promise<UploadedFile[]>((resolve) => {
    let percent = 0
    const timer = setInterval(() => {
      percent = Math.min(100, percent + 25)
      onProgress?.(percent)
      if (percent >= 100) {
        clearInterval(timer)
        resolve(
          files.map((file) => {
            mockFileSeq += 1
            return {
              id: `mock-file-${mockFileSeq}`,
              case_id: caseId,
              kind: 'upload',
              orig_name: file.name,
              mime: file.type || 'application/pdf',
              size: file.size,
              stored_path: `uploads/${caseId}/${file.name}`,
              md_path: null,
              meta: null,
              created_at: new Date().toISOString(),
            }
          }),
        )
      }
    }, 120)
  })
}

/** 多文件上传（一次请求；整体进度）。 */
export function uploadCaseFiles(
  caseId: string,
  files: File[],
  options: UploadOptions = {},
): Promise<UploadedFile[]> {
  if (isMockMode()) return mockUpload(caseId, files, options)
  const { onProgress, signal } = options
  return new Promise<UploadedFile[]>((resolve, reject) => {
    if (signal?.aborted) {
      reject(new ApiError(0, zh.composer.uploadCanceled))
      return
    }

    const xhr = new XMLHttpRequest()
    xhr.open('POST', `${API_BASE}/cases/${encodeURIComponent(caseId)}/files`)
    xhr.responseType = 'json'
    // 上传走的是 XHR 而非 lib/api 的 fetch 封装，CSRF 头得自己补，
    // 否则 M8 之后每次上传都会被后端中间件挡在 403
    const csrf = readCsrfToken()
    if (csrf) xhr.setRequestHeader(CSRF_HEADER, csrf)

    const onAbort = () => xhr.abort()
    signal?.addEventListener('abort', onAbort, { once: true })
    const cleanup = () => signal?.removeEventListener('abort', onAbort)

    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) {
        onProgress(Math.min(100, Math.round((e.loaded / e.total) * 100)))
      }
    }
    xhr.onload = () => {
      cleanup()
      const payload = xhr.response as unknown
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress?.(100)
        resolve(normalizePayload(payload))
      } else {
        // 401 / 必须改密同样要触发全局跳转（此处不经 request() 的统一处理）
        notifyApiAuthFailure(xhr.status, payload)
        reject(new ApiError(xhr.status, extractMessage(xhr.status, payload), payload))
      }
    }
    xhr.onerror = () => {
      cleanup()
      reject(new ApiError(0, zh.common.networkDown))
    }
    xhr.onabort = () => {
      cleanup()
      reject(new ApiError(0, zh.composer.uploadCanceled))
    }

    const form = new FormData()
    for (const file of files) form.append('files', file, file.name)
    xhr.send(form)
  })
}

/** 单文件上传（每文件独立进度时逐个调用）。 */
export async function uploadCaseFile(
  caseId: string,
  file: File,
  options: UploadOptions = {},
): Promise<UploadedFile> {
  const [first] = await uploadCaseFiles(caseId, [file], options)
  if (!first) throw new ApiError(0, zh.composer.uploadEmptyResponse)
  return first
}
