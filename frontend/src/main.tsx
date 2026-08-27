import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './styles/index.css'
import App from './App'
import { initTheme } from './stores/uiStore'

// 启动即套用持久化主题（index.html 内联脚本已做 no-flash 首帧）
initTheme()

/**
 * VITE_USE_MOCKS=1：渲染前先装好 fetch 拦截，避免首批查询漏网。
 * 判断内联写 `import.meta.env.VITE_USE_MOCKS`（不走 isMockMode()），
 * 生产构建才能把整段折为死代码、不产出 mockServer chunk。
 */
async function bootstrap(): Promise<void> {
  if (import.meta.env.VITE_USE_MOCKS === '1') {
    const mocks = await import('./mocks/mockServer')
    mocks.installMockApi()
  }
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  )
}

void bootstrap()
