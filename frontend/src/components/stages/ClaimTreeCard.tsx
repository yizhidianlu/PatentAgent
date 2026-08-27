import { useMemo, useState } from 'react'
import { Squares2X2Icon } from '@heroicons/react/24/outline'
import { zh } from '../../i18n/zh'
import { normalizeClaimTree } from '../../api/reader'
import type { ClaimAmbiguity } from '../../api/reader'
import { Badge } from '../ui/Badge'
import { Select } from '../ui/Select'
import { ClaimTree } from '../reader/ClaimTree'
import { StageCardShell, type StageCardProps } from '../pipeline/StageCardShell'
import { readDefault } from './stagePayload'
import type { InteractionRequiredEvent } from '../../types/stream'

/** 单条疑难项的处理方式（提交给后端 C2 复核）。 */
type ResolutionMode = 'alternative' | 'single'

interface Resolution {
  mode: ResolutionMode
  chosen: number
}

function initialResolution(item: ClaimAmbiguity): Resolution {
  const candidates = item.candidates.length > 0 ? item.candidates : []
  const chosen = item.chosen ?? candidates[0] ?? 0
  // 后端已给出主引用（chosen）→ 默认「指定单一引用」；否则按「或」并列
  return { mode: item.chosen !== null ? 'single' : 'alternative', chosen }
}

/**
 * §3.4 ClaimTreeCard（kind='claim_tree'）：权要树校对卡。
 * 展示解析出的权利要求树 + ambiguities 列表；
 * 每条给「按『或』并列处理」/「指定单一引用」下拉（后者再选具体权项），
 * 底部 确认 / 跳过（StageCardShell 默认底栏）。
 */
export function ClaimTreeCard({ stage, submit, skip, busy }: StageCardProps) {
  const payload = stage.payload as InteractionRequiredEvent | null
  const defaults = readDefault(payload)

  const tree = useMemo(
    () => normalizeClaimTree(defaults, defaults.deltas ?? defaults.claim_deltas),
    [defaults],
  )
  const ambiguities = tree?.ambiguities ?? []

  const [resolutions, setResolutions] = useState<Resolution[]>(() =>
    ambiguities.map(initialResolution),
  )

  const patch = (index: number, next: Partial<Resolution>): void => {
    setResolutions((prev) =>
      prev.map((item, i) => (i === index ? { ...item, ...next } : item)),
    )
  }

  /**
   * 提交格式必须匹配后端 `pipelines/reader.py::_merge_user_tree`（门控 schema
   * `{claims:[{no,type,refs,ref_mode,preamble}], skip}`）：按权号打补丁并回整棵树，
   * 缺 `claims` 会被后端当作「跳过」，用户的消歧选择将被静默丢弃。
   */
  const handleConfirm = (): void => {
    // 1) 疑难项 → 引用补丁（「或」并列 = 全部候选 alternative；单一引用 = 只留所选）
    const patches = new Map<number, { refs: number[]; ref_mode: string }>()
    ambiguities.forEach((item, i) => {
      const resolution = resolutions[i] ?? initialResolution(item)
      if (resolution.mode === 'single') {
        if (resolution.chosen > 0) {
          patches.set(item.claim, { refs: [resolution.chosen], ref_mode: 'single' })
        }
        return
      }
      const candidates = item.candidates.filter((c) => c > 0)
      if (candidates.length > 1) {
        patches.set(item.claim, { refs: candidates, ref_mode: 'alternative' })
      }
    })

    // 2) 整棵树回传（未涉及的权项原样回声，后端据此标记「已消歧」）
    const claims = (tree?.claims ?? []).map((claim) => {
      const patch = patches.get(claim.no)
      return {
        no: claim.no,
        type: claim.type,
        refs: patch?.refs ?? claim.refs,
        ref_mode: patch?.ref_mode ?? claim.refMode,
      }
    })

    submit({ claims, skip: false })
  }

  return (
    <StageCardShell
      stage={stage}
      icon={Squares2X2Icon}
      summary={zh.stages.claimTree.summary(ambiguities.length)}
      onConfirm={handleConfirm}
      onSkip={skip}
      confirmLabel={zh.stages.claimTree.confirm}
      busy={busy}
    >
      <div className="space-y-3">
        <p className="text-sm text-gray-700 dark:text-gray-300">
          {payload?.prompt || zh.stages.claimTree.lead}
        </p>

        {tree && tree.claims.length > 0 && (
          <div>
            <p className="text-[13px] font-medium text-gray-700 dark:text-gray-200 mb-2">
              {zh.stages.claimTree.treeTitle}
            </p>
            <ClaimTree tree={tree} showSummary />
          </div>
        )}

        {ambiguities.length === 0 ? (
          <p className="text-xs text-gray-400 dark:text-gray-500">
            {zh.stages.claimTree.noAmbiguities}
          </p>
        ) : (
          <div className="space-y-2">
            <p className="text-[13px] font-medium text-gray-700 dark:text-gray-200">
              {zh.stages.claimTree.ambiguitiesTitle(ambiguities.length)}
            </p>
            {ambiguities.map((item, index) => {
              const resolution = resolutions[index] ?? initialResolution(item)
              const candidates =
                item.candidates.length > 0
                  ? item.candidates
                  : resolution.chosen
                    ? [resolution.chosen]
                    : []
              return (
                <div
                  key={`${item.claim}-${index}`}
                  className="rounded-xl border border-gray-200 dark:border-gray-700 p-3 space-y-2"
                >
                  <div className="flex items-center gap-2">
                    <span className="text-[13px] font-medium text-gray-900 dark:text-gray-100">
                      {zh.stages.claimTree.claimLabel(item.claim)}
                    </span>
                    <Badge variant="amber">
                      {zh.stages.claimTree.kinds[item.kind] ?? zh.stages.claimTree.kinds.other}
                    </Badge>
                    {candidates.length > 0 && (
                      <span className="text-[11px] text-gray-400 dark:text-gray-500">
                        {zh.stages.claimTree.candidates}：
                        {candidates.map((c) => zh.reader.claimTree.refChip(c)).join(' / ')}
                      </span>
                    )}
                  </div>

                  {item.issue && (
                    <p className="text-[13px] leading-relaxed text-gray-600 dark:text-gray-300">
                      {item.issue}
                    </p>
                  )}
                  {item.reason && (
                    <p className="text-xs leading-relaxed text-gray-400 dark:text-gray-500">
                      {zh.stages.claimTree.reason}：{item.reason}
                    </p>
                  )}

                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-xs text-gray-500 dark:text-gray-400">
                      {zh.stages.claimTree.modeLabel}
                    </span>
                    <Select
                      className="w-44"
                      value={resolution.mode}
                      onChange={(e) => patch(index, { mode: e.target.value as ResolutionMode })}
                      aria-label={zh.stages.claimTree.modeLabel}
                    >
                      <option value="alternative">{zh.stages.claimTree.modeAlternative}</option>
                      <option value="single">{zh.stages.claimTree.modeSingle}</option>
                    </Select>
                    {resolution.mode === 'single' && candidates.length > 0 && (
                      <Select
                        className="w-36"
                        value={String(resolution.chosen)}
                        onChange={(e) => patch(index, { chosen: Number(e.target.value) })}
                        aria-label={zh.stages.claimTree.refLabel}
                      >
                        {candidates.map((c) => (
                          <option key={c} value={c}>
                            {zh.stages.claimTree.refOption(c)}
                          </option>
                        ))}
                      </Select>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </div>
    </StageCardShell>
  )
}
