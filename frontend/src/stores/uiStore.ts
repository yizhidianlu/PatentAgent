import { create } from 'zustand'
import {
  applyTheme,
  readStoredTheme,
  storeTheme,
  type ThemeMode,
} from '../lib/theme'

/** localStorage keys（frontend-design.md §1.1 / §2.2）。 */
const SIDEBAR_COLLAPSED_KEY = 'pa-sidebar-collapsed'
const SIDEBAR_COLLAPSED_GROUPS_KEY = 'pa-sidebar-collapsed-groups'

export type ToastKind = 'success' | 'warning' | 'error' | 'info'

export interface ToastItem {
  id: string
  kind: ToastKind
  message: string
}

function readBool(key: string, fallback: boolean): boolean {
  try {
    const v = localStorage.getItem(key)
    if (v === '1' || v === 'true') return true
    if (v === '0' || v === 'false') return false
  } catch {
    /* ignore */
  }
  return fallback
}

function writeString(key: string, value: string): void {
  try {
    localStorage.setItem(key, value)
  } catch {
    /* ignore */
  }
}

function readStringArray(key: string): string[] {
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return []
    const parsed: unknown = JSON.parse(raw)
    if (Array.isArray(parsed)) return parsed.filter((x): x is string => typeof x === 'string')
  } catch {
    /* ignore */
  }
  return []
}

let toastSeq = 0

export interface UiState {
  /** 主题模式（持久化 pa-theme）。 */
  themeMode: ThemeMode
  setThemeMode: (mode: ThemeMode) => void

  /** 侧栏折叠（持久化 pa-sidebar-collapsed）。 */
  sidebarCollapsed: boolean
  toggleSidebar: () => void
  setSidebarCollapsed: (collapsed: boolean) => void

  /** 分组折叠（持久化 pa-sidebar-collapsed-groups）。 */
  collapsedGroups: string[]
  toggleGroup: (groupKey: string) => void

  /** 移动端抽屉。 */
  mobileDrawerOpen: boolean
  setMobileDrawerOpen: (open: boolean) => void

  /** 头部自动隐藏。 */
  headerHidden: boolean
  setHeaderHidden: (hidden: boolean) => void

  /** Toast 队列。 */
  toasts: ToastItem[]
  pushToast: (kind: ToastKind, message: string) => string
  dismissToast: (id: string) => void
}

export const useUiStore = create<UiState>((set, get) => ({
  themeMode: readStoredTheme(),
  setThemeMode: (mode) => {
    storeTheme(mode)
    applyTheme(mode)
    set({ themeMode: mode })
  },

  sidebarCollapsed: readBool(SIDEBAR_COLLAPSED_KEY, false),
  toggleSidebar: () => get().setSidebarCollapsed(!get().sidebarCollapsed),
  setSidebarCollapsed: (collapsed) => {
    writeString(SIDEBAR_COLLAPSED_KEY, collapsed ? '1' : '0')
    set({ sidebarCollapsed: collapsed })
  },

  collapsedGroups: readStringArray(SIDEBAR_COLLAPSED_GROUPS_KEY),
  toggleGroup: (groupKey) => {
    const current = get().collapsedGroups
    const next = current.includes(groupKey)
      ? current.filter((k) => k !== groupKey)
      : [...current, groupKey]
    writeString(SIDEBAR_COLLAPSED_GROUPS_KEY, JSON.stringify(next))
    set({ collapsedGroups: next })
  },

  mobileDrawerOpen: false,
  setMobileDrawerOpen: (open) => set({ mobileDrawerOpen: open }),

  headerHidden: false,
  setHeaderHidden: (hidden) => set({ headerHidden: hidden }),

  toasts: [],
  pushToast: (kind, message) => {
    toastSeq += 1
    const id = `toast-${Date.now()}-${toastSeq}`
    set((s) => ({ toasts: [...s.toasts, { id, kind, message }] }))
    return id
  },
  dismissToast: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
}))

/** 应用启动时套用持久化主题（main.tsx 调一次）。 */
export function initTheme(): void {
  applyTheme(useUiStore.getState().themeMode)
}
