/**
 * 正文里的图片引用 → 网页端能取到的 URL。
 *
 * 背景：文档正文里的图片写的是**磁盘路径**——交底书插图是上传件的绝对路径，
 * 论文转专利的附图落在 `outputs/{case_id}/p2p_work/`。Word / PDF 导出器直接读盘，
 * 所以导出件里图是全的；浏览器读不了本机路径（`C:` 会被当成未知协议整个丢掉），
 * 网页端于是只剩图题没有图。这里把这类路径改写成 `GET /cases/{id}/media?path=…`。
 *
 * 已经是 http(s)/data/blob 或站内绝对路径的，原样放行——那些本来就能取。
 */

import { API_BASE } from './api'

/** 浏览器本来就能直接取的引用形式。 */
const PASSTHROUGH_RE = /^(https?:|data:|blob:|\/\/)/i

/** 站内绝对路径（含 API 自身返回的下载地址）。 */
function isSiteAbsolute(src: string): boolean {
  return src.startsWith('/') && !src.startsWith('//')
}

/**
 * 把一条图片引用转成可取的 URL；`caseId` 缺失时返回 null（渲染方据此不出图，
 * 而不是渲染一个必然 404 的 `<img>`）。
 */
export function toMediaUrl(caseId: string | null | undefined, src: string): string | null {
  const raw = (src ?? '').trim()
  if (!raw) return null
  if (PASSTHROUGH_RE.test(raw)) return raw
  if (isSiteAbsolute(raw)) return raw
  if (!caseId) return null
  return `${API_BASE}/cases/${encodeURIComponent(caseId)}/media?path=${encodeURIComponent(raw)}`
}
