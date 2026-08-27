import { AnimatePresence, motion } from 'framer-motion'
import { zh } from '../../i18n/zh'
import { Badge } from '../ui/Badge'
import type { HomeModule } from './moduleConfig'

export interface HeroGreetingProps {
  module: HomeModule
}

/**
 * §3.1 HeroGreeting（参考站 Playwright 实测：logo 与标题**同一行**）：
 * `flex items-center justify-center gap-2 sm:gap-3` +
 * logo `w-12 h-12 sm:w-16 sm:h-16 object-contain` +
 * 标题 `text-2xl sm:text-3xl md:text-4xl font-bold tracking-tight` 按模块变文案
 * （AnimatePresence fade y:6）+ 右上 Beta 渐变角标。
 */
export function HeroGreeting({ module }: HeroGreetingProps) {
  return (
    <div className="flex items-center justify-center gap-2 sm:gap-3">
      <img
        src="/logo.svg?v=4"
        alt={zh.brand.logoAlt}
        className="w-12 h-12 sm:w-16 sm:h-16 object-contain shrink-0"
      />
      <div className="relative">
        <AnimatePresence mode="wait" initial={false}>
          <motion.h1
            key={module}
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -6 }}
            transition={{ duration: 0.2 }}
            className="text-2xl sm:text-3xl md:text-4xl font-bold tracking-tight text-gray-900 dark:text-white text-center"
          >
            {zh.home.greetings[module]}
          </motion.h1>
        </AnimatePresence>
        <Badge variant="gradient" className="absolute -top-2 -right-7 sm:-right-9">
          {zh.home.beta}
        </Badge>
      </div>
    </div>
  )
}
