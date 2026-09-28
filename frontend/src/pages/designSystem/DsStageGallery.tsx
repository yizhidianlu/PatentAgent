import { createElement, useMemo } from 'react'
import { mockScripts } from '../../mocks/mockEvents'
import { resolveStageCard, stageCardRegistry } from '../../components/pipeline/stageCardRegistry'
import { StageCardShell } from '../../components/pipeline/StageCardShell'
import type { Stage, StageStatus, InteractionRequiredEvent } from '../../types/stream'

/**
 * 从四套 mock 剧本里抽出全部 interaction_required 载荷，
 * 每个 kind 保留第一条 —— StageCard 画廊的数据源随剧本自动同步，
 * 不必在设计系统页另抄一份 fixture。
 */
function collectPayloads(): { kind: string; payload: InteractionRequiredEvent }[] {
  const byKind = new Map<string, InteractionRequiredEvent>()
  for (const script of mockScripts) {
    for (const item of script.timeline) {
      if (item.event !== 'interaction_required') continue
      const payload = item.data as InteractionRequiredEvent
      if (!payload?.kind || byKind.has(payload.kind)) continue
      byKind.set(payload.kind, payload)
    }
  }
  // 剧本未覆盖到的已注册 kind 也要出现（用最小载荷占位），保证画廊 = 注册表全集
  for (const kind of Object.keys(stageCardRegistry)) {
    if (byKind.has(kind)) continue
    byKind.set(kind, {
      step_key: kind,
      kind,
      prompt: `（剧本未覆盖）${kind} 卡最小载荷预览`,
      schema: null,
      default: undefined,
    } as InteractionRequiredEvent)
  }
  // 未注册 kind 的兜底卡也展一张
  byKind.set('__unknown__', {
    step_key: '__unknown__',
    kind: '__unknown__',
    prompt: '后端新增了前端尚未注册的 kind —— 应兜底为通用 JSON 卡',
    schema: null,
    default: { some_field: '原始载荷会原样展示', count: 3 },
  } as InteractionRequiredEvent)

  return [...byKind.entries()].map(([kind, payload]) => ({ kind, payload }))
}

/**
 * 剧本里走不到、但真后端会发的状态变体。
 *
 * 画廊按 kind 只取剧本里的第一条载荷，而剧本只演成功路径——查新卡的失败态因此从没在
 * 任何页面上被人看到过：提示文案写着「重试检索」，卡片上却没有这个入口，直到真实用户撞上。
 * 载荷形状照抄 backend `disclosure.prior_art_search` 失败门控的 default，改后端时同步这里。
 */
const VARIANTS: { label: string; kind: string; payload: InteractionRequiredEvent }[] = [
  {
    label: 'prior_art · 失败态（超出时间预算，只补检索未完成的词）',
    kind: 'prior_art',
    payload: {
      step_key: 'prior_art_search',
      kind: 'prior_art',
      prompt:
        '检索超出时间预算：已检索的 2 个词均无命中；另有 5 个词未检索（内镜模拟训练、路径判定、自动判题、训练考核装置、错误锁定），时间预算已用尽。国知局本身是通的。「重试」已预填尚未检索的 5 个词，只补检索这些。请选择：重试检索 / 手工补录在先文献 / 跳过查新（跳过时 1.1 会如实写明未进行系统性检索，平台不会编造检索结果）。',
      schema: null,
      default: {
        action: 'retry',
        terms: ['内镜模拟训练', '路径判定', '自动判题', '训练考核装置', '错误锁定'],
        hits: [],
        reason: '',
        failed: true,
        error_message: '已检索的 2 个词均无命中；另有 5 个词未检索',
        failure_kind: 'budget',
      },
    } as InteractionRequiredEvent,
  },
]

function makeStage(
  kind: string,
  payload: InteractionRequiredEvent,
  status: StageStatus,
  suffix = '',
): Stage {
  return {
    id: `ds-${kind}-${status}${suffix}`,
    type: kind,
    status,
    payload,
    stepKey: payload.step_key,
  }
}

const noop = () => undefined

/** 单张卡（active 态；提交/跳过为空操作）。 */
function StageCell({
  kind,
  payload,
  status,
  busy = false,
  variant = '',
}: {
  kind: string
  payload: InteractionRequiredEvent
  status: StageStatus
  busy?: boolean
  variant?: string
}) {
  return createElement(resolveStageCard(kind), {
    caseId: 'ds',
    stage: makeStage(kind, payload, status, `${busy ? '-busy' : ''}${variant}`),
    submit: noop,
    skip: noop,
    busy,
  })
}

/**
 * StageCard 画廊：注册表全部 kind × active 态 + StageCardShell 三态（active /
 * completed / skipped / busy）。像素 QA 时对照本页逐张核对头条、体、底栏与折叠行。
 */
export function DsStageGallery() {
  const entries = useMemo(() => collectPayloads(), [])
  const sample = entries.find((e) => e.kind === 'intake') ?? entries[0]

  return (
    <div className="space-y-8">
      <section className="space-y-3">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-400 dark:text-gray-500">
          StageCardShell 四态（active / busy / completed / skipped）
        </h3>
        <div className="space-y-3">
          <StageCell kind={sample.kind} payload={sample.payload} status="active" />
          <StageCell kind={sample.kind} payload={sample.payload} status="active" busy />
          <StageCardShell
            stage={makeStage('intake', sample.payload, 'completed', '-shell')}
            summary="已确认边界：发明 · 便携式术后康复监测装置"
          >
            <p className="text-sm text-gray-600 dark:text-gray-300">
              折叠行可点开重展为只读（fieldset disabled）。
            </p>
          </StageCardShell>
          <StageCardShell stage={makeStage('intake', sample.payload, 'skipped', '-shell')} />
        </div>
      </section>

      <section className="space-y-3">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-400 dark:text-gray-500">
          全部 StageCard（{entries.length} 种 kind，active 态）
        </h3>
        <div className="space-y-4">
          {entries.map(({ kind, payload }) => (
            <div key={kind} className="space-y-1.5">
              <code className="text-[11px] text-gray-400 dark:text-gray-500">kind={kind}</code>
              <StageCell kind={kind} payload={payload} status="active" />
            </div>
          ))}
        </div>
      </section>

      <section className="space-y-3">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-400 dark:text-gray-500">
          状态变体（剧本走不到、真后端会发）
        </h3>
        <div className="space-y-4">
          {VARIANTS.map(({ label, kind, payload }, i) => (
            <div key={label} className="space-y-1.5">
              <code className="text-[11px] text-gray-400 dark:text-gray-500">{label}</code>
              <StageCell kind={kind} payload={payload} status="active" variant={`-v${i}`} />
            </div>
          ))}
        </div>
      </section>
    </div>
  )
}
