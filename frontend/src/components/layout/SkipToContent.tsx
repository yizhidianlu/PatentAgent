import { zh } from '../../i18n/zh'

/** `<main>` 的锚点 id（跳转链接与布局共用）。 */
export const MAIN_CONTENT_ID = 'main-content'

/**
 * 「跳到主内容」跳过链接（a11y 基础）：
 * 侧栏会话多时，键盘用户到达 composer 需要 Tab 十几次；本链接是页面第一个可聚焦元素，
 * 平时视觉隐藏（不是 display:none，否则无法被 Tab 命中），聚焦后浮出左上角。
 */
export function SkipToContent() {
  return (
    <a
      href={`#${MAIN_CONTENT_ID}`}
      onClick={(e) => {
        // 让焦点真正落到 main 上（仅靠 hash 在 SPA 里不可靠）
        e.preventDefault()
        const main = document.getElementById(MAIN_CONTENT_ID)
        main?.focus()
        main?.scrollTo({ top: 0 })
      }}
      className={
        'sr-only focus:not-sr-only focus:fixed focus:left-3 focus:top-3 ' +
        'focus:z-[var(--z-tooltip)] focus:rounded-xl focus:px-4 focus:py-2 ' +
        'focus:bg-[#6366f1] focus:text-white focus:text-sm focus:font-medium focus:shadow-lg ' +
        'focus:outline-none focus:ring-4 focus:ring-indigo-500/30'
      }
    >
      {zh.common.skipToContent}
    </a>
  )
}
