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
}: {
  kind: string
  payload: InteractionRequiredEvent
  status: StageStatus
  busy?: boolean
}) {
  return createElement(resolveStageCard(kind), {
    caseId: 'ds',
    stage: makeStage(kind, payload, status, busy ? '-busy' : ''),
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
    </div>
  )
}
