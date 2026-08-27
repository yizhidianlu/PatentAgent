import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../lib/api'
import { pipelineKeys } from './pipeline'

/**
 * 交底书迭代（模块 A / A-迭代）：
 * POST /api/v1/cases/{id}/disclosure/iterate {mode,instruction,file_ids} → 202
 * - mode='auto'：由后端 IT0 意图路由判定 merge（合并新材料）/ correct（纠正）；
 * - 后端未就绪时返回 404，调用方据此提示「迭代能力尚未就绪」并保留输入。
 */

/** 迭代模式：auto = 交后端意图路由判定。 */
export type DisclosureIterateMode = 'auto' | 'merge' | 'correct'

export interface DisclosureIterateInput {
  mode?: DisclosureIterateMode
  instruction: string
  file_ids?: string[]
}

export interface DisclosureIterateResult {
  run_group?: string
  intent?: string
}

/** 该错误是否代表「后端尚未实现迭代端点」。 */
export function isIterateUnavailable(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 404 || error.status === 405)
}

export function useDisclosureIterate(caseId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (input: DisclosureIterateInput) =>
      api.post<DisclosureIterateResult>(
        `/cases/${encodeURIComponent(caseId)}/disclosure/iterate`,
        { mode: 'auto', file_ids: [], ...input },
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: pipelineKeys.state(caseId) })
    },
  })
}
