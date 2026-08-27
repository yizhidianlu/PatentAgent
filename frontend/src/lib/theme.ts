/**
 * 主题系统（frontend-design.md §1.1）
 * ThemeMode 四态：light / dark / system / auto（18:00–06:00 深色）。
 * - applyTheme：换 <html> class（dark/light）+ data-theme
 * - system：订阅 matchMedia('(prefers-color-scheme: dark)') change
 * - auto：setTimeout 到下一个 18:00 / 06:00 边界重估
 */

export type ThemeMode = 'light' | 'dark' | 'system' | 'auto'

export const THEME_STORAGE_KEY = 'pa-theme'

const AUTO_DARK_START_HOUR = 18
const AUTO_DARK_END_HOUR = 6

let mediaQuery: MediaQueryList | null = null
let mediaListener: ((e: MediaQueryListEvent) => void) | null = null
let autoTimer: ReturnType<typeof setTimeout> | null = null

/** 当前时刻按 auto 规则（18:00–06:00 深色）应否为深色。 */
export function isAutoDarkNow(now: Date = new Date()): boolean {
  const h = now.getHours()
  return h >= AUTO_DARK_START_HOUR || h < AUTO_DARK_END_HOUR
}

/** 解析某模式此刻的实际明暗。 */
export function resolveIsDark(mode: ThemeMode): boolean {
  switch (mode) {
    case 'dark':
      return true
    case 'light':
      return false
    case 'auto':
      return isAutoDarkNow()
    case 'system':
    default:
      return window.matchMedia('(prefers-color-scheme: dark)').matches
  }
}

function setDocumentTheme(dark: boolean): void {
  const el = document.documentElement
  el.classList.remove('dark', 'light')
  el.classList.add(dark ? 'dark' : 'light')
  el.dataset.theme = dark ? 'dark' : 'light'
}

/** 距下一个 18:00 / 06:00 边界的毫秒数。 */
function msUntilNextAutoBoundary(now: Date = new Date()): number {
  const next = new Date(now)
  next.setMinutes(0, 0, 0)
  const h = now.getHours()
  if (h < AUTO_DARK_END_HOUR) {
    next.setHours(AUTO_DARK_END_HOUR)
  } else if (h < AUTO_DARK_START_HOUR) {
    next.setHours(AUTO_DARK_START_HOUR)
  } else {
    next.setDate(next.getDate() + 1)
    next.setHours(AUTO_DARK_END_HOUR)
  }
  return Math.max(next.getTime() - now.getTime(), 1000)
}

function clearSubscriptions(): void {
  if (mediaQuery && mediaListener) {
    mediaQuery.removeEventListener('change', mediaListener)
  }
  mediaQuery = null
  mediaListener = null
  if (autoTimer !== null) {
    clearTimeout(autoTimer)
    autoTimer = null
  }
}

/**
 * 应用主题模式：立即生效，并按模式建立后续订阅
 * （system → matchMedia change；auto → 定时到下一边界重估）。
 */
export function applyTheme(mode: ThemeMode): void {
  clearSubscriptions()
  setDocumentTheme(resolveIsDark(mode))

  if (mode === 'system') {
    mediaQuery = window.matchMedia('(prefers-color-scheme: dark)')
    mediaListener = (e: MediaQueryListEvent) => setDocumentTheme(e.matches)
    mediaQuery.addEventListener('change', mediaListener)
  } else if (mode === 'auto') {
    const schedule = () => {
      autoTimer = setTimeout(() => {
        setDocumentTheme(isAutoDarkNow())
        schedule()
      }, msUntilNextAutoBoundary())
    }
    schedule()
  }
}

/** 从 localStorage 读取模式（非法值回退 system）。 */
export function readStoredTheme(): ThemeMode {
  try {
    const v = localStorage.getItem(THEME_STORAGE_KEY)
    if (v === 'light' || v === 'dark' || v === 'system' || v === 'auto') return v
  } catch {
    /* localStorage 不可用时静默回退 */
  }
  return 'system'
}

/** 持久化模式到 localStorage。 */
export function storeTheme(mode: ThemeMode): void {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, mode)
  } catch {
    /* localStorage 不可用时静默忽略 */
  }
}
