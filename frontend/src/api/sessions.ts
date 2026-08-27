import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../lib/api'

/**
 * 案件（会话）react-query hooks（backend-architecture.md §API）：
 * GET /cases?module= 列表（侧栏）、POST /cases 建案。
 */

export type Module = 'disclosure' | 'paper2patent' | 'reader' | 'oa'

export type PatentType = 'invention' | 'utility_model' | 'design'

export type CaseStatus =
  | 'draft'
  | 'running'
  | 'waiting_user'
  | 'completed'
  | 'failed'
  | 'archived'

export interface Contact {
  name: string
  phone: string
  email: string
}

/** 后端 CaseOut（models/case.py）。 */
export interface CaseOut {
  id: string
  module: Module
  title: string
  patent_type: PatentType | null
  status: CaseStatus
  current_step: string | null
  contact: Contact | null
  created_at: string
  updated_at: string
}

/** 后端分页壳 Page[T]（models/common.py）。 */
export interface Page<T> {
  items: T[]
  total: number
  limit: number
  offset: number
}

export interface CaseCreatePayload {
  module: Module
  title?: string
  patent_type?: PatentType
}

/** react-query key 约定：['cases', module | 'all']。 */
export const caseKeys = {
  all: ['cases'] as const,
  list: (module?: Module) => ['cases', module ?? 'all'] as const,
}

/** 侧栏案件列表（按模块过滤；updated_at 倒序）。 */
export function useCases(module?: Module) {
  return useQuery({
    queryKey: caseKeys.list(module),
    queryFn: () =>
      api.get<Page<CaseOut>>('/cases', { query: { module, limit: 100 } }),
  })
}

/** POST /cases 建案；成功后失效全部案件列表。 */
export function useCreateCase() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: CaseCreatePayload) => api.post<CaseOut>('/cases', payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: caseKeys.all })
    },
  })
}

/** PATCH /cases/{id} 改名（侧栏 … 菜单）。 */
export function useRenameCase() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ id, title }: { id: string; title: string }) =>
      api.patch<CaseOut>(`/cases/${encodeURIComponent(id)}`, { title }),
    onSuccess: (data) => {
      void queryClient.invalidateQueries({ queryKey: caseKeys.all })
      if (data?.id) void queryClient.invalidateQueries({ queryKey: ['case', data.id] })
    },
  })
}

/** DELETE /cases/{id} 删除案件（级联）。 */
export function useDeleteCase() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => api.delete<unknown>(`/cases/${encodeURIComponent(id)}`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: caseKeys.all })
    },
  })
}
