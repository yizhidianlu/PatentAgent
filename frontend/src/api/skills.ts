/**
 * 技能库 react-query hooks。
 *
 * | 方法 | 路径 | 说明 |
 * |---|---|---|
 * | GET | `/skills` | 全部技能 + 实时可用性 + 启用状态 |
 * | PUT | `/skills/{key}` | 开关某项技能（管理员） |
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../lib/api'

export type SkillCategory = 'search' | 'drawing' | 'writing' | 'validation' | 'export'

/**
 * needs_config = 去设置页填一下就有；
 * unavailable  = 本机环境不具备（没装 Word/Chrome），用户自己在界面上解决不了。
 * 这两者要分开显示，否则用户会对着一个「不可用」反复点设置页。
 */
export type SkillStatus = 'available' | 'needs_config' | 'unavailable'

export type SkillModule = 'disclosure' | 'paper2patent' | 'reader' | 'oa'
export type SkillPatentType = 'invention' | 'utility_model' | 'design'

export interface SkillRequirement {
  key: string
  label: string
  satisfied: boolean
  hint: string
  settings_path: string | null
}

export interface Skill {
  key: string
  name: string
  category: SkillCategory
  summary: string
  description: string
  modules: SkillModule[]
  patent_types: SkillPatentType[]
  status: SkillStatus
  requirements: SkillRequirement[]
  enabled: boolean
  /** false = 流程骨架的一部分，关掉就等于允许产出不合规文书，不给开关 */
  toggleable: boolean
  inputs: string
  outputs: string
  provider: string
  source_url: string | null
  license: string | null
  cost_hint: string
}

export interface SkillList {
  skills: Skill[]
  categories: { key: string; label: string }[]
}

export const skillKeys = {
  all: ['skills'] as const,
}

export function useSkills() {
  return useQuery({
    queryKey: skillKeys.all,
    queryFn: () => api.get<SkillList>('/skills'),
    // 不设 staleTime：用户在设置页配完模型回到技能库，应当立刻看到状态变了
    staleTime: 0,
  })
}

export function useToggleSkill() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ key, enabled }: { key: string; enabled: boolean }) =>
      api.put<Skill>(`/skills/${encodeURIComponent(key)}`, { enabled }),
    onSuccess: (updated) => {
      queryClient.setQueryData<SkillList>(skillKeys.all, (prev) =>
        prev
          ? { ...prev, skills: prev.skills.map((s) => (s.key === updated.key ? updated : s)) }
          : prev,
      )
    },
  })
}
