import { AnimatePresence, motion } from 'framer-motion'
import { ArrowUpTrayIcon } from '@heroicons/react/24/outline'
import { zh } from '../../i18n/zh'
import { cn } from '../../lib/cn'

export interface DragOverlayProps {
  visible: boolean
  /** 覆盖层圆角需与所在外壳一致（hero 外壳 rounded-3xl / chat rounded-2xl）。 */
  className?: string
}

/**
 * §2.3 拖放覆盖层：`absolute inset-0 z-20 rounded-2xl backdrop-blur-sm`，
 * 居中 ArrowUpTrayIcon +「拖放文件到此处上传」。pointer-events-none 让 drop 落到外壳。
 */
export function DragOverlay({ visible, className }: DragOverlayProps) {
  return (
    <AnimatePresence>
      {visible && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.15 }}
          className={cn(
            'absolute inset-0 z-20 rounded-2xl backdrop-blur-sm bg-white/70 dark:bg-gray-900/70',
            'flex flex-col items-center justify-center gap-2 pointer-events-none',
            className,
          )}
        >
          <ArrowUpTrayIcon className="w-8 h-8 text-blue-500" strokeWidth={1.5} />
          <span className="text-sm font-medium text-blue-600 dark:text-blue-400">
            {zh.composer.dropHint}
          </span>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
