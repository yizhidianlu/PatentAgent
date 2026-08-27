import type { ComponentType } from 'react'
import type { StageCardProps } from './StageCardShell'
import { ClaimBiasCard } from '../stages/ClaimBiasCard'
import { ClaimTreeCard } from '../stages/ClaimTreeCard'
import { ConfirmCard } from '../stages/ConfirmCard'
import { DeliveryCard } from '../stages/DeliveryCard'
import { FiguresPreviewCard } from '../stages/FiguresPreviewCard'
import { FormCard } from '../stages/FormCard'
import { IntakeCard } from '../stages/IntakeCard'
import { MaterialUploadCard } from '../stages/MaterialUploadCard'
import { ModeSelectCard } from '../stages/ModeSelectCard'
import { OaIssuesCard } from '../stages/OaIssuesCard'
import { OaRetrieveCard } from '../stages/OaRetrieveCard'
import { OaStrategyCard } from '../stages/OaStrategyCard'
import { SchemaFillCard } from '../stages/SchemaFillCard'
import { PatentPointsCard } from '../stages/PatentPointsCard'
import { PreviewConfirmCard } from '../stages/PreviewConfirmCard'
import { PriorArtCard } from '../stages/PriorArtCard'
import { ReviewCard } from '../stages/ReviewCard'
import { SelfCheckCard } from '../stages/SelfCheckCard'
import { TypeSuggestCard } from '../stages/TypeSuggestCard'
import { UnknownStageCard } from '../stages/UnknownStageCard'

/**
 * §4 StageCard 注册表：interaction_required.kind → 卡片组件。
 * 未注册的 kind 一律兜底 UnknownStageCard（prompt + 原始载荷 JSON + 确认/跳过），
 * 保证后端扩展 kind 时前端不至于渲染空白。
 */
export const stageCardRegistry: Record<string, ComponentType<StageCardProps>> = {
  // 通用（schema 驱动表单 / 纯确认）
  form: FormCard,
  confirm: ConfirmCard,

  // 交底书（§2.8 模块 A 专用卡；kind='form' 且 schema.hint=intake 时 FormCard 改派 IntakeCard）
  intake: IntakeCard,
  type_suggest: TypeSuggestCard,
  material_upload: MaterialUploadCard,
  patent_points: PatentPointsCard,
  prior_art: PriorArtCard,
  preview_confirm: PreviewConfirmCard,
  self_check: SelfCheckCard,
  claim_bias: ClaimBiasCard,

  // 论文转专利
  mode_select: ModeSelectCard,
  review: ReviewCard,
  content_review: ReviewCard,
  figures_preview: FiguresPreviewCard,

  // 专利解读（§3.4 模块 C）
  claim_tree: ClaimTreeCard,

  // 交底书实用新型 / 外观设计分支（A3b 填表与线稿）
  schema_fill: SchemaFillCard,

  // 审查答复（§3.5 三处强制人审：问题核对 / 案例勾选 / 策略选择）
  oa_issues: OaIssuesCard,
  oa_retrieve: OaRetrieveCard,
  oa_strategy: OaStrategyCard,

  // 交付（四模块共用）
  delivery: DeliveryCard,
}

/** 按 kind 取卡片组件；未知 kind 兜底 UnknownStageCard。 */
export function resolveStageCard(kind: string): ComponentType<StageCardProps> {
  return stageCardRegistry[kind] ?? UnknownStageCard
}
