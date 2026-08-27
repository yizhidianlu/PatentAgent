import { useEffect, useRef, useState } from 'react'
import { motion } from 'framer-motion'
import { zh } from '../../i18n/zh'
import { ChipPreviewCard } from './ChipPreviewCard'
import { MODULE_META, type ChipMeta, type HomeModule } from './moduleConfig'

/** chip 点击产生的动作：预填 composer 或触发上传。 */
export type ChipActionEvent =
  | { type: 'prefill'; text: string }
  | { type: 'upload'; kind: 'file' | 'image' }

export interface FeatureChipsProps {
  module: HomeModule
  onAction: (event: ChipActionEvent) => void
}

interface ChipCopy {
  label: string
  title: string
  desc: string
  prefill: string
}

const HOVER_PREVIEW_DELAY_MS = 300

/**
 * §2.5 FeatureChips：`flex flex-wrap gap-2 justify-center` + motion stagger i*.05；
 * chip `px-3 py-1.5 rounded-full border text-xs font-medium hover:border-[#6366f1]/40`；
 * 悬停 300ms 出 ChipPreviewCard；点击预填 composer 或触发上传。
 */
export function FeatureChips({ module, onAction }: FeatureChipsProps) {
  const [activeChip, setActiveChip] = useState<string | null>(null)
  const hoverTimer = useRef<number | null>(null)

  const clearTimer = () => {
    if (hoverTimer.current !== null) {
      window.clearTimeout(hoverTimer.current)
      hoverTimer.current = null
    }
  }

  useEffect(() => () => clearTimer(), [])
  useEffect(() => {
    setActiveChip(null)
  }, [module])

  const copyMap = zh.home.chips[module] as unknown as Record<string, ChipCopy>
  const chips = MODULE_META[module].chips

  const handleEnter = (id: string) => {
    clearTimer()
    hoverTimer.current = window.setTimeout(
      () => setActiveChip(id),
      HOVER_PREVIEW_DELAY_MS,
    )
  }

  const handleLeave = () => {
    clearTimer()
    setActiveChip(null)
  }

  const handleClick = (chip: ChipMeta, copy: ChipCopy) => {
    handleLeave()
    if (chip.action === 'prefill') {
      onAction({ type: 'prefill', text: copy.prefill })
    } else {
      onAction({ type: 'upload', kind: chip.action === 'upload-image' ? 'image' : 'file' })
    }
  }

  return (
    // key=module：切模块整行重挂载，重新播放 stagger 入场
    <div key={module} className="w-full flex flex-wrap gap-2 justify-center">
      {chips.map((chip, i) => {
        const copy = copyMap[chip.id]
        if (!copy) return null
        const Icon = chip.icon
        return (
          <motion.div
            key={chip.id}
            className="relative"
            initial={{ opacity: 0, y: 4 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.2, delay: i * 0.05 }}
            onMouseEnter={() => handleEnter(chip.id)}
            onMouseLeave={handleLeave}
          >
            <button
              type="button"
              onClick={() => handleClick(chip, copy)}
              className={
                'inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full border border-gray-200 dark:border-gray-700 ' +
                'bg-white dark:bg-gray-800 text-xs font-medium text-gray-700 dark:text-gray-200 ' +
                'hover:border-[#6366f1]/40 hover:shadow-sm transition-all'
              }
            >
              <Icon className="w-3.5 h-3.5 text-gray-400" strokeWidth={1.5} />
              {copy.label}
            </button>
            {activeChip === chip.id && (
              <ChipPreviewCard title={copy.title} description={copy.desc} />
            )}
          </motion.div>
        )
      })}
    </div>
  )
}
