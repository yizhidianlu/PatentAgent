import { useMemo, useState, type ComponentType, type SVGProps } from 'react'
import { motion } from 'framer-motion'
import { Link, useNavigate } from 'react-router-dom'
import {
  AcademicCapIcon,
  BookOpenIcon,
  ChatBubbleLeftRightIcon,
  ChevronDoubleLeftIcon,
  Cog6ToothIcon,
  DocumentTextIcon,
  ExclamationTriangleIcon,
  MagnifyingGlassIcon,
  PlusIcon,
} from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { useUiStore } from '../../stores/uiStore'
import { MQ_DESKTOP, MQ_TABLET_LANDSCAPE, useMediaQuery } from '../../lib/useMediaQuery'
import { useCases, type CaseOut, type Module } from '../../api/sessions'
import { Skeleton } from '../ui/Skeleton'
import { SidebarGroup, type SidebarSessionItem } from './SidebarGroup'

export interface SidebarGroupData {
  key: string
  label: string
  basePath: string
  Icon: ComponentType<SVGProps<SVGSVGElement>>
  items: SidebarSessionItem[]
}

/** 分组骨架：后端 module 枚举 → 侧栏分组 / 工作台路由前缀。 */
const GROUP_DEFS: { key: string; module: Module; label: string; basePath: string; Icon: ComponentType<SVGProps<SVGSVGElement>> }[] = [
  {
    key: 'disclosure',
    module: 'disclosure',
    label: zh.sidebar.groups.disclosure,
    basePath: '/disclosure',
    Icon: DocumentTextIcon,
  },
  {
    key: 'paper',
    module: 'paper2patent',
    label: zh.sidebar.groups.paper,
    basePath: '/paper',
    Icon: AcademicCapIcon,
  },
  {
    key: 'reader',
    module: 'reader',
    label: zh.sidebar.groups.reader,
    basePath: '/reader',
    Icon: BookOpenIcon,
  },
  {
    key: 'oa',
    module: 'oa',
    label: zh.sidebar.groups.oa,
    basePath: '/oa',
    Icon: ChatBubbleLeftRightIcon,
  },
]

/** GET /cases 结果 → 四个侧栏分组（updated_at 倒序由后端保证）。 */
function groupCases(cases: CaseOut[]): SidebarGroupData[] {
  return GROUP_DEFS.map(({ key, module, label, basePath, Icon }) => ({
    key,
    label,
    basePath,
    Icon,
    items: cases
      .filter((c) => c.module === module)
      .map<SidebarSessionItem>((c) => ({
        id: c.id,
        title: c.title || zh.sidebar.untitled,
        // 后端 waiting_user = 有待确认的 HITL 门控（§4 侧栏红点）
        pendingConfirm: c.status === 'waiting_user',
      })),
  }))
}

/** 当前侧栏占用宽度（px）：<768px 为 0（走 MobileDrawer）。AppLayout 用它同步主内容 padding-left。 */
export function useSidebarWidth(): number {
  const isDesktop = useMediaQuery(MQ_DESKTOP)
  const isTabletLandscape = useMediaQuery(MQ_TABLET_LANDSCAPE)
  const collapsed = useUiStore((s) => s.sidebarCollapsed)
  if (!isDesktop) return 0
  if (collapsed) return 72
  return isTabletLandscape ? 200 : 260
}

/** 折叠态图标列与展开态共用的会话分组数据。 */
function useSidebarGroups() {
  const query = useCases()
  const groups = useMemo(() => groupCases(query.data?.items ?? []), [query.data])
  return { query, groups }
}

/** 侧栏内容（展开态）；MobileDrawer 复用。 */
export function SidebarExpandedContent({ onNavigate }: { onNavigate?: () => void }) {
  const navigate = useNavigate()
  const [query, setQuery] = useState('')
  const { query: casesQuery, groups: allGroups } = useSidebarGroups()

  const q = query.trim().toLowerCase()
  const groups = q
    ? allGroups.map((g) => ({
        ...g,
        items: g.items.filter((it) => it.title.toLowerCase().includes(q)),
      }))
    : allGroups
  const totalItems = groups.reduce((n, g) => n + g.items.length, 0)
  const totalAll = allGroups.reduce((n, g) => n + g.items.length, 0)

  return (
    <>
      {/* 顶块：新建会话 + 搜索 */}
      <div className="p-3 space-y-2 shrink-0">
        <button
          type="button"
          onClick={() => {
            navigate('/')
            onNavigate?.()
          }}
          className="w-full h-9 flex items-center gap-2 px-3 rounded-xl bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 shadow-sm text-sm font-medium text-gray-700 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-700 active:scale-[0.99] transition-all"
        >
          <PlusIcon className="w-4 h-4" strokeWidth={1.5} />
          {zh.sidebar.newSession}
        </button>
        <div className="relative">
          <MagnifyingGlassIcon
            className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400"
            strokeWidth={1.5}
          />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={zh.sidebar.searchPlaceholder}
            aria-label={zh.sidebar.searchPlaceholder}
            className="w-full h-9 rounded-lg pl-8 pr-2 text-[13px] bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 text-gray-700 dark:text-gray-200 placeholder:text-gray-400 dark:placeholder:text-gray-500 outline-none focus:border-[#6366f1] transition-colors"
          />
        </div>
      </div>

      {/* 分组列表 */}
      <div className="flex-1 overflow-y-auto scrollbar-thin px-2">
        {casesQuery.isPending ? (
          // 页面级骨架（§3 加载态）：组头 + 三条会话行
          <div className="px-1 pt-1 space-y-4" aria-busy="true" aria-label={zh.sidebar.loading}>
            {[0, 1].map((block) => (
              <div key={block} className="space-y-1.5">
                <Skeleton shimmer className="h-4 w-20 rounded" />
                <Skeleton shimmer className="h-7 w-full rounded-lg" />
                <Skeleton shimmer className="h-7 w-[85%] rounded-lg" />
              </div>
            ))}
          </div>
        ) : casesQuery.isError ? (
          <div className="mt-4 mx-1 px-3 py-3 rounded-xl border border-amber-200 dark:border-amber-500/30 bg-amber-50 dark:bg-amber-500/10 text-[13px] text-amber-700 dark:text-amber-400">
            <p className="flex items-start gap-1.5 font-medium">
              <ExclamationTriangleIcon className="w-4 h-4 shrink-0 mt-px" strokeWidth={1.5} />
              <span className="min-w-0">{zh.sidebar.loadFailed}</span>
            </p>
            <button
              type="button"
              onClick={() => void casesQuery.refetch()}
              className="mt-1.5 text-xs font-medium underline underline-offset-2 hover:no-underline"
            >
              {zh.common.retry}
            </button>
          </div>
        ) : (
          <>
            {groups.map((g) => (
              <SidebarGroup
                key={g.key}
                groupKey={g.key}
                label={g.label}
                basePath={g.basePath}
                items={g.items}
                onNavigate={onNavigate}
              />
            ))}
            {totalItems === 0 && (
              <div className="mt-4 mx-1 px-3 py-3.5 rounded-xl border border-[#492497]/20 text-[13px] text-gray-600 dark:text-gray-400">
                <p className="font-medium mb-0.5">
                  {totalAll === 0 ? zh.sidebar.empty.title : zh.sidebar.searchEmpty.title}
                </p>
                <p className="text-xs text-gray-500 dark:text-gray-500">
                  {totalAll === 0
                    ? zh.sidebar.empty.description
                    : zh.sidebar.searchEmpty.description}
                </p>
              </div>
            )}
          </>
        )}
      </div>
    </>
  )
}

/** §2.2 Sidebar：桌面固定侧栏，motion 宽度 260⇄72（平板横屏 200⇄72）。 */
export function Sidebar() {
  const collapsed = useUiStore((s) => s.sidebarCollapsed)
  const toggleSidebar = useUiStore((s) => s.toggleSidebar)
  const navigate = useNavigate()
  const isDesktop = useMediaQuery(MQ_DESKTOP)
  const width = useSidebarWidth()
  const { groups } = useSidebarGroups()

  if (!isDesktop) return null

  return (
    <motion.aside
      animate={{ width }}
      initial={false}
      transition={{ duration: 0.3, ease: [0.25, 0.1, 0.25, 1] }}
      aria-label={zh.sidebar.navLabel}
      className="fixed top-[var(--app-header-h)] left-0 h-[calc(100vh-var(--app-header-h))] bg-gray-50 dark:bg-gray-900 border-r border-gray-100 dark:border-gray-800 z-40 flex flex-col overflow-hidden"
    >
      {collapsed ? (
        <>
          {/* 折叠态：icon-only */}
          <div className="p-3 shrink-0">
            <button
              type="button"
              onClick={() => navigate('/')}
              title={zh.sidebar.newSession}
              aria-label={zh.sidebar.newSession}
              className="w-10 h-10 mx-auto flex items-center justify-center rounded-xl bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 shadow-sm text-gray-700 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-700 active:scale-[0.99] transition-all"
            >
              <PlusIcon className="w-4 h-4" strokeWidth={1.5} />
            </button>
          </div>
          <div className="flex-1 overflow-y-auto scrollbar-thin px-2 flex flex-col items-center gap-1">
            {groups.map(({ key, label, Icon, items }) => {
              const groupLabel = zh.sidebar.groupCount(label, items.length)
              return (
                <button
                  key={key}
                  type="button"
                  title={groupLabel}
                  aria-label={groupLabel}
                  onClick={() => toggleSidebar()}
                  className="relative w-10 h-10 flex items-center justify-center rounded-lg text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors"
                >
                  <Icon className="w-5 h-5" strokeWidth={1.5} />
                  {items.some((it) => it.pendingConfirm) && (
                    <span
                      className="absolute top-1.5 right-1.5 w-2 h-2 rounded-full bg-red-500 ring-2 ring-gray-50 dark:ring-gray-900"
                      aria-hidden="true"
                    />
                  )}
                </button>
              )
            })}
          </div>
        </>
      ) : (
        <SidebarExpandedContent />
      )}

      {/* 底块：设置行 + 折叠钮 */}
      <div className="p-3 border-t border-gray-100 dark:border-gray-800 shrink-0 flex flex-col gap-1">
        <Link
          to="/settings"
          title={zh.sidebar.settings}
          aria-label={collapsed ? zh.sidebar.settings : undefined}
          className={cn(
            'flex items-center gap-2 rounded-lg text-[13px] text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors',
            collapsed ? 'w-10 h-10 mx-auto justify-center' : 'px-2 py-2',
          )}
        >
          <Cog6ToothIcon className="w-5 h-5 shrink-0" strokeWidth={1.5} />
          {!collapsed && <span className="truncate">{zh.sidebar.settings}</span>}
        </Link>
        <button
          type="button"
          onClick={toggleSidebar}
          title={collapsed ? zh.sidebar.expand : zh.sidebar.collapse}
          aria-label={collapsed ? zh.sidebar.expand : zh.sidebar.collapse}
          className={cn(
            'flex items-center gap-2 rounded-lg text-[13px] text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors',
            collapsed ? 'w-10 h-10 mx-auto justify-center' : 'px-2 py-2',
          )}
        >
          <ChevronDoubleLeftIcon
            className={cn('w-5 h-5 shrink-0 transition-transform duration-300', collapsed && 'rotate-180')}
            strokeWidth={1.5}
          />
          {!collapsed && <span className="truncate">{zh.sidebar.collapse}</span>}
        </button>
      </div>
    </motion.aside>
  )
}
