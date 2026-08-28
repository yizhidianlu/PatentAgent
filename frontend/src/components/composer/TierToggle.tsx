import { BoltIcon, SparklesIcon } from '@heroicons/react/24/outline'
import { motion } from 'framer-motion'
import { useId } from 'react'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { useModelTiers } from '../../api/settings'
import type { ModelTier } from '../../api/sessions'

const TIERS: { key: ModelTier; label: string; hint: string; icon: typeof BoltIcon }[] = [
  { key: 'fast', label: zh.tier.fast, hint: zh.tier.fastHint, icon: BoltIcon },
  { key: 'deep', label: zh.tier.deep, hint: zh.tier.deepHint, icon: SparklesIcon },
]

export interface TierToggleProps {
  value: ModelTier
  onChange: (tier: ModelTier) => void
  /** 换档请求在途时禁用，避免连点造成来回横跳。 */
  busy?: boolean
  className?: string
}

/**
 * 聊天框上的模型档位开关。
 *
 * **为什么把「实际用哪个模型」写进 title 而不是直接显示出来。**
 * 档位名（快速 / 深度思考）是用户要做的选择，模型名（`qwen-plus`）是实现细节；
 * 但两档如果指向同一个模型，用户按下去会以为发生了什么而其实什么也没变——
 * 所以模型名必须**能查到**，只是不占位置。悬停即见。
 *
 * 两档都没配时整个开关不渲染：给一个按下去没有任何区别的开关，
 * 比不给更糟。
 */
export function TierToggle({ value, onChange, busy, className }: TierToggleProps) {
  const tiersQuery = useModelTiers()
  const layoutId = useId()

  const data = tiersQuery.data
  // 两档指向同一个模型 = 这个开关没有意义，不渲染。
  // 拿不到配置时也不渲染：宁可没有开关，也不要一个说不清自己在做什么的开关。
  const fastModel = data?.effective?.fast ?? ''
  const deepModel = data?.effective?.deep ?? ''
  if (!data || !fastModel || !deepModel || fastModel === deepModel) return null

  const modelOf = (tier: ModelTier): string => (tier === 'fast' ? fastModel : deepModel)
  const configured = (tier: ModelTier): boolean => Boolean(data[tier]?.model?.trim())

  return (
    <div
      role="radiogroup"
      aria-label={zh.tier.label}
      className={cn(
        'relative inline-flex items-center gap-0.5 rounded-full p-0.5',
        'bg-gray-100 dark:bg-gray-800',
        busy && 'opacity-60 pointer-events-none',
        className,
      )}
    >
      {TIERS.map((tier) => {
        const active = tier.key === value
        const label = data[tier.key]?.label?.trim() || tier.label
        const Icon = tier.icon
        return (
          <button
            key={tier.key}
            type="button"
            role="radio"
            aria-checked={active}
            title={`${tier.hint}\n${
              configured(tier.key) ? zh.tier.usingModel(modelOf(tier.key)) : zh.tier.notConfigured
            }`}
            onClick={() => {
              if (!active) onChange(tier.key)
            }}
            className={cn(
              'relative z-10 flex items-center gap-1 rounded-full px-2.5 py-1 text-xs',
              'transition-colors duration-200',
              active
                ? 'text-gray-900 dark:text-gray-50'
                : 'text-gray-500 hover:text-gray-700 dark:hover:text-gray-300',
            )}
          >
            {active && (
              <motion.span
                layoutId={`tier-thumb-${layoutId}`}
                transition={{ duration: 0.25, ease: [0.25, 0.1, 0.25, 1] }}
                className="absolute inset-0 -z-10 rounded-full bg-white dark:bg-gray-700 shadow-sm"
              />
            )}
            <Icon className="w-3.5 h-3.5 shrink-0" strokeWidth={1.5} />
            <span className="whitespace-nowrap">{label}</span>
          </button>
        )
      })}
    </div>
  )
}
