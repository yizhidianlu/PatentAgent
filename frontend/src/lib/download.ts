import { API_BASE } from './api'

/** 浏览器端下载工具（版本历史 / 交付物 / 附图导出等处复用，单一来源）。 */

/** 文档下载格式（DownloadMenu / DeliveryCard 等处共用）。 */
export type DownloadFormat = 'md' | 'docx' | 'pdf' | 'json'

/** 通过临时 <a download> 触发浏览器下载。 */
export function downloadUrl(url: string, filename?: string): void {
  const anchor = document.createElement('a')
  anchor.href = url
  if (filename) anchor.download = filename
  anchor.rel = 'noopener'
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
}

/** 下载内存中的 Blob（自动创建并回收 object URL）。 */
export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  try {
    downloadUrl(url, filename)
  } finally {
    // 延迟回收，确保点击已被浏览器消费
    window.setTimeout(() => URL.revokeObjectURL(url), 1_000)
  }
}

/** 下载文本内容（默认 UTF-8 纯文本；导出 Markdown 时传 text/markdown）。 */
export function downloadText(
  text: string,
  filename: string,
  mime = 'text/plain;charset=utf-8',
): void {
  downloadBlob(new Blob([text], { type: mime }), filename)
}

/** 全部可选下载格式（DownloadMenu 缺省渲染这一组）。 */
export const ALL_DOWNLOAD_FORMATS: readonly DownloadFormat[] = ['md', 'docx', 'pdf', 'json']

function extOf(filename: string | undefined): string {
  if (!filename) return ''
  const dot = filename.lastIndexOf('.')
  return dot >= 0 ? filename.slice(dot + 1).toLowerCase() : ''
}

/**
 * 从一版 doc_version 的 downloads 清单推出「真的能下」的格式。
 * 四个模块交付的格式并不一致（交底书 MD/DOCX/PDF、论文转专利 JSON/DOCX/PDF、
 * 审查答复 MD/DOCX），照着固定三格渲染就会摆出点了只弹「尚未生成」的死钮。
 * 清单为空时返回 undefined，交由调用方回落到默认全集（老数据兼容）。
 */
export function availableFormats(
  downloads: readonly { kind?: unknown; filename?: string }[] | undefined,
  fallbackFilename?: string,
): readonly DownloadFormat[] | undefined {
  const found = new Set<DownloadFormat>()
  for (const entry of downloads ?? []) {
    for (const format of ALL_DOWNLOAD_FORMATS) {
      if (String(entry.kind ?? '').endsWith(format) || extOf(entry.filename) === format) {
        found.add(format)
      }
    }
  }
  if (found.size === 0) {
    const ext = extOf(fallbackFilename)
    const hit = ALL_DOWNLOAD_FORMATS.find((f) => f === ext)
    if (hit) return [hit]
    return undefined
  }
  return ALL_DOWNLOAD_FORMATS.filter((f) => found.has(f))
}

/** 交付物下载地址（backend §3.8：GET /artifacts/{id}/download）。 */
export function artifactDownloadUrl(artifactId: string): string {
  return `${API_BASE}/artifacts/${encodeURIComponent(artifactId)}/download`
}

/** 原始上传件下载地址：GET /api/v1/files/{id}/download。 */
export function fileDownloadUrl(fileId: string): string {
  return `${API_BASE}/files/${encodeURIComponent(fileId)}/download`
}

/** 转换后 Markdown 全文地址：GET /api/v1/files/{id}/content。 */
export function fileContentUrl(fileId: string): string {
  return `${API_BASE}/files/${encodeURIComponent(fileId)}/content`
}
