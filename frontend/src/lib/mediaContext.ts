/**
 * 当前正文所属案件 id。
 *
 * Markdown 里的图片引用是磁盘路径，要换成 `/cases/{id}/media` 才能在浏览器里取到，
 * 而 MarkdownBlock 位于组件树很深处（DocumentPanel / StreamItemView / ReportView
 * 各有一条路径）。逐层传 prop 会把这件事摊到七八个与图片无关的组件签名上，
 * 所以用 context 在工作台外壳处一次性给出。
 *
 * 不在 context 里（案例库详情、设置页预览这类没有案件上下文的地方）时值为 null，
 * 图片引用会被丢弃而不是渲染成必然 404 的 `<img>`。
 */

import { createContext, useContext } from 'react'

export const MediaCaseContext = createContext<string | null>(null)

export function useMediaCaseId(): string | null {
  return useContext(MediaCaseContext)
}
