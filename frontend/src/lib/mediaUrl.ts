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
 * markdown 的链接目标已经被百分号编码过一次，这里先解回来。
 *
 * CommonMark 规定链接目标要做 URL 规范化，所以 `![图](patent_图1.png)` 到了
 * 渲染器手里已经是 `patent_%E5%9B%BE1.png`。再 `encodeURIComponent` 一次，
 * `%` 会被转成 `%25`，服务端解一次只得到 `patent_%E5%9B%BE1.png` 这个字面名字——
 * **文件名里带汉字的图，每一张都会 404**。附图文件名清一色是「patent_图N.png」，
 * 所以现象是「一张图都不显示」。
 *
 * 解不动（畸形转义序列）就原样用：宁可按原样去试，也不要在这里抛异常。
 */
function decodeOnce(value: string): string {
  try {
    return decodeURIComponent(value)
  } catch {
    return value
  }
}

/**
 * 把一条图片引用转成可取的 URL；`caseId` 缺失时返回 null（渲染方据此显示占位，
 * 而不是渲染一个必然 404 的 `<img>`）。
 */
export function toMediaUrl(caseId: string | null | undefined, src: string): string | null {
  const raw = (src ?? '').trim()
  if (!raw) return null
  if (PASSTHROUGH_RE.test(raw)) return raw
  if (isSiteAbsolute(raw)) return raw
  if (!caseId) return null
  const path = decodeOnce(raw)
  return `${API_BASE}/cases/${encodeURIComponent(caseId)}/media?path=${encodeURIComponent(path)}`
}
