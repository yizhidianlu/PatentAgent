/**
 * StageCard 载荷读取工具（§4）：
 * 后端 interaction_required.default / schema 字段命名可能演进，
 * 各卡片一律经此处的宽松取值工具读取，缺字段时降级为空值而非崩溃。
 */

import type { InteractionRequiredEvent } from '../../types/stream'

export function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

export function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

/** 任意值 → 展示字符串（数字/布尔转文本，数组以「、」连接，对象取常见文本字段）。 */
export function str(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  if (Array.isArray(value)) return value.map(str).filter(Boolean).join('、')
  const record = asRecord(value)
  if (record) {
    for (const key of ['text', 'label', 'title', 'name', 'content', 'value']) {
      const v = record[key]
      if (typeof v === 'string' && v.trim()) return v
    }
  }
  return ''
}

export function num(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string' && value.trim() && Number.isFinite(Number(value))) {
    return Number(value)
  }
  return null
}

/** interaction_required.default（非对象时给空对象）。 */
export function readDefault(payload: InteractionRequiredEvent | null): Record<string, unknown> {
  return asRecord(payload?.default) ?? {}
}

/** interaction_required.schema（非对象时给空对象）。 */
export function readSchema(payload: InteractionRequiredEvent | null): Record<string, unknown> {
  return asRecord(payload?.schema) ?? {}
}

/** 按候选键顺序取第一个非空字符串。 */
export function pickStr(record: Record<string, unknown> | null, ...keys: string[]): string {
  if (!record) return ''
  for (const key of keys) {
    const text = str(record[key])
    if (text.trim()) return text
  }
  return ''
}

/** 按候选键顺序取第一个数组。 */
export function pickArray(record: Record<string, unknown> | null, ...keys: string[]): unknown[] {
  if (!record) return []
  for (const key of keys) {
    if (Array.isArray(record[key])) return record[key] as unknown[]
  }
  return []
}

/** 按候选键顺序取第一个存在的布尔值。 */
export function pickBool(
  record: Record<string, unknown> | null,
  keys: string[],
  fallback = false,
): boolean {
  if (!record) return fallback
  for (const key of keys) {
    const v = record[key]
    if (typeof v === 'boolean') return v
    if (v === 'true') return true
    if (v === 'false') return false
  }
  return fallback
}

/** 字符串数组（元素逐个 str 化并去空）。 */
export function strList(value: unknown): string[] {
  return asArray(value)
    .map(str)
    .filter((s) => s.trim().length > 0)
}
