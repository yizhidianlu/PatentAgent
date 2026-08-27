import { useEffect, useState } from 'react'

/** 订阅 CSS media query（SSR 安全：初值即时求值）。 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState<boolean>(() =>
    typeof window !== 'undefined' ? window.matchMedia(query).matches : false,
  )

  useEffect(() => {
    const mql = window.matchMedia(query)
    const onChange = (e: MediaQueryListEvent) => setMatches(e.matches)
    setMatches(mql.matches)
    mql.addEventListener('change', onChange)
    return () => mql.removeEventListener('change', onChange)
  }, [query])

  return matches
}

/** ≥768px：桌面/平板（侧栏常驻；以下用 MobileDrawer）。 */
export const MQ_DESKTOP = '(min-width: 768px)'
/** 平板横屏（§2.2 侧栏 200⇄72）。 */
export const MQ_TABLET_LANDSCAPE =
  '(min-width: 768px) and (max-width: 1366px) and (orientation: landscape)'
