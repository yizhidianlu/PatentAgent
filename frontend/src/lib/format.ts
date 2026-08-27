/** 时间 / 文件大小格式化工具。 */

function pad2(n: number): string {
  return n < 10 ? `0${n}` : String(n)
}

/** `2026-08-25 14:32` 风格时间戳（版本历史等处使用）。 */
export function formatDateTime(input: Date | string | number): string {
  const d = input instanceof Date ? input : new Date(input)
  if (Number.isNaN(d.getTime())) return ''
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`
}

/** `2026-08-25` 风格日期。 */
export function formatDate(input: Date | string | number): string {
  const d = input instanceof Date ? input : new Date(input)
  if (Number.isNaN(d.getTime())) return ''
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`
}

/** 相对时间：刚刚 / n 分钟前 / n 小时前 / 昨天 / 日期。 */
export function formatRelativeTime(input: Date | string | number): string {
  const d = input instanceof Date ? input : new Date(input)
  if (Number.isNaN(d.getTime())) return ''
  const diffMs = Date.now() - d.getTime()
  if (diffMs < 60_000) return '刚刚'
  const minutes = Math.floor(diffMs / 60_000)
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  const days = Math.floor(hours / 24)
  if (days === 1) return '昨天'
  if (days < 7) return `${days} 天前`
  return formatDate(d)
}

/** 文件大小：B / KB / MB / GB，保留一位小数（整数省略小数位）。 */
export function formatFileSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return ''
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB', 'TB'] as const
  let value = bytes
  let unitIndex = -1
  do {
    value /= 1024
    unitIndex += 1
  } while (value >= 1024 && unitIndex < units.length - 1)
  const rounded = value >= 10 || Number.isInteger(value) ? Math.round(value) : value.toFixed(1)
  return `${rounded} ${units[unitIndex]}`
}

/** 秒数 → `812ms` / `1.2s` / `1分30秒`。 */
export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return ''
  if (ms < 1000) return `${Math.round(ms)}ms`
  const seconds = ms / 1000
  if (seconds < 60) return `${seconds >= 10 ? Math.round(seconds) : seconds.toFixed(1)}s`
  const minutes = Math.floor(seconds / 60)
  const rest = Math.round(seconds % 60)
  return rest === 0 ? `${minutes}分钟` : `${minutes}分${rest}秒`
}
