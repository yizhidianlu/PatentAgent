import { Outlet } from 'react-router-dom'
import { AppHeader } from './AppHeader'
import { MAIN_CONTENT_ID, SkipToContent } from './SkipToContent'

/**
 * SecondaryLayout（/oa/cases、/settings）：AppHeader + 全宽滚动内容，无侧栏。
 */
export function SecondaryLayout() {
  return (
    <div className="h-screen bg-white dark:bg-gray-900 flex flex-col overflow-hidden">
      <SkipToContent />
      <AppHeader showMobileMenuButton={false} />
      <main
        id={MAIN_CONTENT_ID}
        tabIndex={-1}
        className="flex-1 overflow-y-auto flex flex-col outline-none"
      >
        <Outlet />
      </main>
    </div>
  )
}
