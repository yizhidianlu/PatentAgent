import { useEffect, useRef, useState } from 'react'
import { CheckIcon, ClipboardIcon } from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'

export interface CodeBlockProps {
  code: string
  language?: string
  className?: string
}

/**
 * §2.7 CodeBlock：`rounded-xl bg-gray-900 text-gray-100 text-[13px] p-4 overflow-x-auto`
 * + 悬停显示复制钮（成功态 1.6s 打勾）。
 */
export function CodeBlock({ code, language, className }: CodeBlockProps) {
  const [copied, setCopied] = useState(false)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(
    () => () => {
      if (timerRef.current !== null) clearTimeout(timerRef.current)
    },
    [],
  )

  const copy = async (): Promise<void> => {
    try {
      await navigator.clipboard.writeText(code)
      setCopied(true)
      if (timerRef.current !== null) clearTimeout(timerRef.current)
      timerRef.current = setTimeout(() => setCopied(false), 1600)
    } catch {
      /* clipboard 不可用时静默失败 */
    }
  }

  return (
    <div className={cn('md-codeblock group relative my-4', className)}>
      {language && (
        <span className="absolute left-3 top-2 text-[10px] font-medium uppercase tracking-wider text-gray-500 select-none">
          {language}
        </span>
      )}
      <button
        type="button"
        onClick={() => void copy()}
        aria-label={copied ? zh.markdown.copied : zh.markdown.copy}
        className={cn(
          'absolute right-2 top-2 z-10 flex items-center gap-1 rounded-lg px-2 py-1 text-[11px] font-medium',
          'bg-gray-800 text-gray-300 border border-gray-700 transition-opacity',
          'opacity-0 group-hover:opacity-100 focus-visible:opacity-100 focus-visible:outline-none',
          copied && 'opacity-100 text-emerald-400',
        )}
      >
        {copied ? (
          <CheckIcon className="w-3.5 h-3.5" strokeWidth={2} />
        ) : (
          <ClipboardIcon className="w-3.5 h-3.5" strokeWidth={1.5} />
        )}
        {copied ? zh.markdown.copied : zh.markdown.copy}
      </button>
      <pre className="rounded-xl bg-gray-900 text-gray-100 text-[13px] leading-relaxed p-4 pt-7 overflow-x-auto scrollbar-thin">
        <code>{code}</code>
      </pre>
    </div>
  )
}
