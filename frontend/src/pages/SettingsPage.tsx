import { useMemo, useState, type ComponentType, type SVGProps } from 'react'
import {
  CircleStackIcon,
  CpuChipIcon,
  KeyIcon,
  PaintBrushIcon,
  PhotoIcon,
  ShieldExclamationIcon,
} from '@heroicons/react/24/outline'
import { cn } from '../lib/cn'
import { zh } from '../i18n/zh'
import { useIsAdmin } from '../stores/authStore'
import { ModelSection } from './settings/ModelSection'
import { EmbeddingSection } from './settings/EmbeddingSection'
import { ImageGenSection } from './settings/ImageGenSection'
import { AppearanceSection } from './settings/AppearanceSection'
import { PasswordSection } from './settings/PasswordSection'

type SectionKey = 'model' | 'embedding' | 'imageGen' | 'appearance' | 'password'

interface SectionDef {
  key: SectionKey
  label: string
  Icon: ComponentType<SVGProps<SVGSVGElement>>
  /** 平台级设置：仅管理员可见，且顶部要挂 amber 提示条（§4.3）。 */
  platform?: boolean
}

/** 平台级三节（模型 / 向量 / 图像）——后端已把它们收紧为 require_admin。 */
const PLATFORM_SECTIONS: SectionDef[] = [
  { key: 'model', label: zh.settings.nav.model, Icon: CpuChipIcon, platform: true },
  { key: 'embedding', label: zh.settings.nav.embedding, Icon: CircleStackIcon, platform: true },
  { key: 'imageGen', label: zh.settings.nav.imageGen, Icon: PhotoIcon, platform: true },
]

/** 人人可见的两节。 */
const PERSONAL_SECTIONS: SectionDef[] = [
  { key: 'appearance', label: zh.settings.nav.appearance, Icon: PaintBrushIcon },
  { key: 'password', label: zh.settings.nav.password, Icon: KeyIcon },
]

/** 平台级设置顶部的 amber 提示条。 */
function PlatformNotice() {
  return (
    <div className="flex items-start gap-2 rounded-xl border border-amber-200 dark:border-amber-500/30 bg-amber-50 dark:bg-amber-500/10 px-3.5 py-3">
      <ShieldExclamationIcon className="w-5 h-5 shrink-0 text-amber-500" strokeWidth={1.5} />
      <div className="min-w-0">
        <p className="text-sm font-semibold text-amber-700 dark:text-amber-400">
          {zh.settings.platformNotice}
        </p>
        <p className="mt-0.5 text-xs text-amber-700/80 dark:text-amber-400/80">
          {zh.settings.platformNoticeDesc}
        </p>
      </div>
    </div>
  )
}

/**
 * §3.7 设置页：左侧 w-64 导航 aside（移动端横向 pill tabs）+ `max-w-2xl space-y-6` 内容区。
 *
 * M8 起按角色分岔（auth-system.md §4.3）：
 * - 普通用户只见「外观」「修改密码」；
 * - 管理员额外见「模型服务」「向量与检索」「图像生成」，并在这三节顶部标注
 *   「平台级设置，对全部用户生效」。
 *
 * 前端隐藏只是体验，后端对这三个 settings 端点挂了 require_admin 做兜底。
 */
export function SettingsPage() {
  const isAdmin = useIsAdmin()
  const sections = useMemo<SectionDef[]>(
    () => (isAdmin ? [...PLATFORM_SECTIONS, ...PERSONAL_SECTIONS] : PERSONAL_SECTIONS),
    [isAdmin],
  )

  const [requested, setRequested] = useState<SectionKey | null>(null)
  // 角色变化（或首帧 role 尚未就绪）时，落到可见列表里的第一节，避免渲染空白内容区
  const active = sections.some((s) => s.key === requested)
    ? (requested as SectionKey)
    : sections[0].key
  const activeDef = sections.find((s) => s.key === active)

  return (
    <div className="flex-1 min-h-0 flex">
      <aside className="hidden lg:block w-64 shrink-0 bg-white dark:bg-gray-800 border-r border-gray-200 dark:border-gray-700 py-4 overflow-y-auto">
        <h1 className="px-6 pb-2 text-[11px] font-semibold uppercase tracking-wide text-gray-400 dark:text-gray-500">
          {zh.settings.title}
        </h1>
        <nav aria-label={zh.settings.title}>
          {sections.map(({ key, label, Icon }) => (
            <button
              key={key}
              type="button"
              aria-current={active === key ? 'page' : undefined}
              onClick={() => setRequested(key)}
              className={cn(
                'w-[calc(100%-1rem)] mx-2 px-4 py-2.5 rounded-lg text-sm flex items-center gap-3 transition-colors',
                'hover:bg-gray-50 dark:hover:bg-gray-700/50',
                active === key
                  ? 'bg-indigo-50 dark:bg-indigo-500/10 text-[#6366f1] font-medium'
                  : 'text-gray-600 dark:text-gray-300',
              )}
            >
              <Icon className="w-5 h-5 shrink-0" strokeWidth={1.5} />
              {label}
            </button>
          ))}
        </nav>
      </aside>

      <div className="flex-1 min-w-0 overflow-y-auto">
        {/* 移动端：横向 pill tabs */}
        <div className="lg:hidden sticky top-0 z-10 bg-white/80 dark:bg-gray-900/80 backdrop-blur-md border-b border-gray-200 dark:border-gray-700 px-4 py-3 space-y-2">
          <h1 className="text-base font-bold tracking-tight text-gray-900 dark:text-gray-100">
            {zh.settings.title}
          </h1>
          <nav
            aria-label={zh.settings.title}
            className="flex items-center gap-2 overflow-x-auto scrollbar-thin"
          >
            {sections.map(({ key, label, Icon }) => (
              <button
                key={key}
                type="button"
                aria-current={active === key ? 'page' : undefined}
                onClick={() => setRequested(key)}
                className={cn(
                  'shrink-0 inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-xs font-medium transition-colors',
                  active === key
                    ? 'bg-indigo-50 dark:bg-indigo-500/10 text-[#6366f1]'
                    : 'bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400',
                )}
              >
                <Icon className="w-3.5 h-3.5" strokeWidth={1.5} />
                {label}
              </button>
            ))}
          </nav>
        </div>

        <div className="max-w-2xl space-y-6 px-4 sm:px-8 py-6">
          {activeDef?.platform && <PlatformNotice />}
          {active === 'model' && <ModelSection />}
          {active === 'embedding' && <EmbeddingSection />}
          {active === 'imageGen' && <ImageGenSection />}
          {active === 'appearance' && <AppearanceSection />}
          {active === 'password' && <PasswordSection />}
        </div>
      </div>
    </div>
  )
}
