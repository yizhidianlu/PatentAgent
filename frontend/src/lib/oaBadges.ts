/**
 * 审查答复模块的徽章工具（OaRetrieveCard 与 OACasesPage 共用）：
 * 结案结果配色（授权 emerald / 驳回 red / 审中 amber）与相似度百分比归一。
 */

import type { BadgeVariant } from '../components/ui/Badge'

/** 结案结果（models/oa.py::OUTCOMES）→ 徽章配色。 */
export function outcomeBadgeVariant(outcome: string): BadgeVariant {
  if (outcome === 'granted' || outcome === 'amended_then_granted') return 'emerald'
  if (outcome === 'rejected') return 'red'
  if (outcome === 'pending') return 'amber'
  return 'neutral'
}

/** 0–1 余弦分与已是百分数两种形态都归一到 0–100 整数。 */
export function scorePercent(score: number): number {
  const value = score > 1 ? score : score * 100
  return Math.max(0, Math.min(100, Math.round(value)))
}
