import { forwardRef, useEffect, useImperativeHandle, useRef } from 'react'
import { cn } from '../../lib/cn'

export interface ComposerInputHandle {
  focus: () => void
}

export interface ComposerInputProps {
  value: string
  onChange: (text: string) => void
  /** Enter（非 Shift、非 IME 合成中）触发。 */
  onSubmit: () => void
  placeholder: string
  onFocusChange?: (focused: boolean) => void
  disabled?: boolean
  className?: string
}

/** contentEditable 读数规整：浏览器常在末尾多挂一个 \n。 */
function readText(el: HTMLElement): string {
  const text = el.innerText
  return text.endsWith('\n') ? text.slice(0, -1) : text
}

/**
 * §2.3 ComposerInput：contentEditable 输入区。
 * - placeholder 用 `empty:before:content-[attr(data-placeholder)]` 技巧；
 * - 粘贴纯文本；Enter 发送 / Shift+Enter 换行；IME isComposing 安全。
 */
export const ComposerInput = forwardRef<ComposerInputHandle, ComposerInputProps>(
  function ComposerInput(
    { value, onChange, onSubmit, placeholder, onFocusChange, disabled = false, className },
    ref,
  ) {
    const divRef = useRef<HTMLDivElement>(null)

    useImperativeHandle(ref, () => ({
      focus: () => {
        const el = divRef.current
        if (!el) return
        el.focus()
        // 光标移到末尾（预填后接着输入）
        const range = document.createRange()
        range.selectNodeContents(el)
        range.collapse(false)
        const selection = window.getSelection()
        selection?.removeAllRanges()
        selection?.addRange(range)
      },
    }))

    // 外部 value 变化（预填 / 发送后清空）时同步 DOM；打字过程中两者一致，不动光标
    useEffect(() => {
      const el = divRef.current
      if (!el) return
      if (readText(el) !== value) {
        if (value === '') el.innerHTML = ''
        else el.innerText = value
      }
    }, [value])

    const handleInput = () => {
      const el = divRef.current
      if (!el) return
      const text = readText(el)
      if (text === '') {
        // 清掉删空后残留的 <br>，恢复 :empty 让 placeholder 生效
        if (el.innerHTML !== '') el.innerHTML = ''
        onChange('')
        return
      }
      onChange(text)
    }

    const handleKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
      if (e.key !== 'Enter' || e.shiftKey) return
      /*
       * IME 候选确认的 Enter 不发送。
       * isComposing 之外再看 keyCode===229：部分中/日/韩输入法（尤其 Windows 上的
       * 微软拼音与搜狗）在候选确认这一下会先派 compositionend 再派 keydown，
       * 此时 isComposing 已经是 false，只认它就会把「选词」当成「发送」——
       * 用户敲下确认候选，半句话被直接发出去。229 是这类合成键的通用哨兵值。
       */
      if (e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229) return
      e.preventDefault()
      onSubmit()
    }

    const handlePaste = (e: React.ClipboardEvent<HTMLDivElement>) => {
      e.preventDefault()
      const text = e.clipboardData.getData('text/plain')
      if (text) document.execCommand('insertText', false, text)
    }

    return (
      <div
        ref={divRef}
        contentEditable={!disabled}
        role="textbox"
        aria-multiline="true"
        aria-label={placeholder}
        data-placeholder={placeholder}
        onInput={handleInput}
        onKeyDown={handleKeyDown}
        onPaste={handlePaste}
        onFocus={() => onFocusChange?.(true)}
        onBlur={() => onFocusChange?.(false)}
        className={cn(
          'w-full text-base leading-relaxed min-h-[1.75rem] max-h-[200px] overflow-y-auto outline-none scrollbar-thin',
          'text-gray-900 dark:text-gray-100 whitespace-pre-wrap break-words',
          'empty:before:content-[attr(data-placeholder)] before:text-gray-400 dark:before:text-gray-500 before:pointer-events-none',
          disabled && 'opacity-60',
          className,
        )}
      />
    )
  },
)
