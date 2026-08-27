import type { ComponentType, SVGProps } from 'react'
import {
  ArrowsRightLeftIcon,
  ArrowUpTrayIcon,
  BeakerIcon,
  BoltIcon,
  BookOpenIcon,
  ChatBubbleLeftRightIcon,
  DocumentArrowUpIcon,
  DocumentTextIcon,
  GlobeAltIcon,
  HandRaisedIcon,
  HashtagIcon,
  LightBulbIcon,
  MagnifyingGlassIcon,
  PencilSquareIcon,
  PhotoIcon,
  ScaleIcon,
  ShareIcon,
} from '@heroicons/react/24/outline'
import type { Module } from '../../api/sessions'

/**
 * 首页模块配置（frontend-design.md §2.5 / §3.1）：
 * 四模块 → 后端 module 枚举 / 工作台路由 / accent / accept / chips。
 */

export type HomeModule = 'disclosure' | 'paper' | 'reader' | 'oa'

export const HOME_MODULES = ['disclosure', 'paper', 'reader', 'oa'] as const

export type HeroIcon = ComponentType<SVGProps<SVGSVGElement>>

export type ChipActionKind = 'prefill' | 'upload-file' | 'upload-image'

export interface ChipMeta {
  /** 对应 zh.home.chips[module] 的键。 */
  id: string
  icon: HeroIcon
  action: ChipActionKind
}

export interface HomeModuleMeta {
  backendModule: Module
  /** 工作台路由前缀（模块→路由映射 disclosure/paper/reader/oa）。 */
  routeBase: '/disclosure' | '/paper' | '/reader' | '/oa'
  /** ModuleToggle 分段图标（Composer 工具栏内，简称 + 图标）。 */
  icon: HeroIcon
  accent: 'indigo' | 'orange'
  fileAccept: string
  imageAccept: string
  chips: ChipMeta[]
}

const IMAGE_ACCEPT = 'image/png,image/jpeg,image/webp,image/gif'

export const MODULE_META: Record<HomeModule, HomeModuleMeta> = {
  disclosure: {
    backendModule: 'disclosure',
    routeBase: '/disclosure',
    icon: PencilSquareIcon,
    accent: 'indigo',
    fileAccept: '.pdf,.doc,.docx,.ppt,.pptx,.md,.txt',
    imageAccept: IMAGE_ACCEPT,
    chips: [
      { id: 'uploadMaterial', icon: ArrowUpTrayIcon, action: 'upload-file' },
      { id: 'minePoints', icon: LightBulbIcon, action: 'prefill' },
      { id: 'noveltySearch', icon: GlobeAltIcon, action: 'prefill' },
      { id: 'generateDoc', icon: DocumentTextIcon, action: 'prefill' },
      { id: 'sampleCase', icon: BeakerIcon, action: 'prefill' },
    ],
  },
  paper: {
    backendModule: 'paper2patent',
    routeBase: '/paper',
    icon: ArrowsRightLeftIcon,
    accent: 'orange',
    fileAccept: '.pdf',
    imageAccept: IMAGE_ACCEPT,
    chips: [
      { id: 'uploadPaper', icon: DocumentArrowUpIcon, action: 'upload-file' },
      { id: 'directGenerate', icon: BoltIcon, action: 'prefill' },
      { id: 'confirmMode', icon: HandRaisedIcon, action: 'prefill' },
      { id: 'figuresPreview', icon: PhotoIcon, action: 'prefill' },
    ],
  },
  reader: {
    backendModule: 'reader',
    routeBase: '/reader',
    icon: BookOpenIcon,
    accent: 'indigo',
    fileAccept: '.pdf',
    imageAccept: IMAGE_ACCEPT,
    chips: [
      { id: 'publicationNo', icon: HashtagIcon, action: 'prefill' },
      { id: 'uploadPatent', icon: DocumentArrowUpIcon, action: 'upload-file' },
      { id: 'claimTree', icon: ShareIcon, action: 'prefill' },
      { id: 'plainReport', icon: BookOpenIcon, action: 'prefill' },
    ],
  },
  oa: {
    backendModule: 'oa',
    routeBase: '/oa',
    icon: ChatBubbleLeftRightIcon,
    accent: 'indigo',
    fileAccept: '.pdf,.doc,.docx',
    imageAccept: IMAGE_ACCEPT,
    chips: [
      { id: 'uploadNotice', icon: DocumentArrowUpIcon, action: 'upload-file' },
      { id: 'strategy', icon: ScaleIcon, action: 'prefill' },
      { id: 'caseSearch', icon: MagnifyingGlassIcon, action: 'prefill' },
    ],
  },
}

/** 模块选择持久化（§2.4：localStorage pa-home-module）。 */
const HOME_MODULE_KEY = 'pa-home-module'

export function readStoredModule(): HomeModule {
  try {
    const raw = localStorage.getItem(HOME_MODULE_KEY)
    if (raw && (HOME_MODULES as readonly string[]).includes(raw)) return raw as HomeModule
  } catch {
    /* ignore */
  }
  return 'disclosure'
}

export function storeModule(module: HomeModule): void {
  try {
    localStorage.setItem(HOME_MODULE_KEY, module)
  } catch {
    /* ignore */
  }
}
