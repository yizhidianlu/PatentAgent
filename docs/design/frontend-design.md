# 引途医疗专利智能体 — 前端实现计划（像素级复刻 fojiaoai.cn/dashboard）

> 由规划 Agent 产出，2026-08-25。目标目录 `frontend/`。React SPA + Vite + TypeScript + Tailwind CSS v4 + framer-motion + Heroicons 24/outline。全部 UI 文案简体中文。视觉规格从参考站发布的 CSS bundle（`c614aee94fe4a183.css`）与 JS chunks（4159 header / 5820 sidebar / 2540 composer / 8350 secondary layout）逐类名提取。
> **产品名（用户已定）：引途医疗专利智能体** — 用于 header 字标、`<title>` 等一切品牌位。

## 0. 依赖与脚手架

`npm create vite@latest frontend -- --template react-ts`

依赖：react / react-dom / react-router-dom(v7) / tailwindcss + @tailwindcss/vite / tailwindcss-animate（若 v4 加载失败换 tw-animate-css；用到的工具类：`animate-in fade-in zoom-in-95 slide-in-from-bottom-2 slide-in-from-right duration-200`）/ framer-motion(v11) / @heroicons/react(24/outline, stroke 1.5) / zustand + @tanstack/react-query / react-markdown + remark-gfm + remark-math + rehype-katex + katex / mermaid（动态导入）/ clsx + tailwind-merge。

vite.config.ts：react 插件 + tailwindcss() + dev proxy `/api → http://127.0.0.1:8000`；build.outDir='dist'（FastAPI 静态托管 + SPA fallback）。

### 0.1 文件树

```
frontend/
├── index.html                      # no-flash 主题脚本 + body 渐变 class + <title>引途医疗专利智能体</title>
├── vite.config.ts  package.json  tsconfig.json
├── public/logo.svg                 # 品牌标（渐变圆角方块占位）
└── src/
    ├── main.tsx  App.tsx
    ├── routes/router.tsx
    ├── styles/index.css            # @theme tokens、variants、keyframes、滚动条、glass、base
    ├── i18n/zh.ts                  # 全部 UI 文案单一来源
    ├── lib/  cn.ts  api.ts  sse.ts  theme.ts  format.ts  download.ts
    ├── types/  models.ts  stream.ts  stages.ts
    ├── stores/  uiStore.ts  sessionStore.ts  composerStore.ts
    ├── api/  sessions.ts  uploads.ts  versions.ts  settings.ts  cases.ts   # react-query hooks
    ├── mocks/  mockEvents.ts  mockServer.ts      # VITE_USE_MOCKS=1 脚本化 SSE 时间线
    ├── components/
    │   ├── layout/   AppLayout AppHeader Sidebar SidebarGroup MobileDrawer SecondaryLayout
    │   ├── ui/       Button Card Modal Dropdown Drawer Toast ToggleSwitch SegmentedToggle Badge Skeleton EmptyState Spinner Input Select
    │   ├── theme/    ThemeToggle
    │   ├── composer/ Composer ComposerInput FilePill PlusMenu SendButton DragOverlay
    │   ├── home/     HeroGreeting ModuleToggle ActivityPill FeatureChips ChipPreviewCard
    │   ├── markdown/ StreamingMarkdown MarkdownBlock MermaidBlock CodeBlock
    │   ├── pipeline/ PipelineStream StreamItemView StageCardShell stageCardRegistry StepProgress
    │   ├── stages/   IntakeFormCard TypeSuggestCard MaterialUploadCard PatentPointsCard PriorArtCard
    │   │             PreviewConfirmCard ClaimBiasCard SelfCheckCard ModeSelectCard OAIssuesCard
    │   │             OAStrategyCard FiguresPreviewCard DeliveryCard
    │   ├── document/ DocumentPanel DocumentCard VersionHistory DownloadMenu
    │   ├── upload/   Dropzone
    │   └── reader/   ClaimTree ReportToc
    └── pages/
        ├── HomePage  DisclosurePage  PaperPage  ReaderPage  OAPage  OACasesPage
        ├── SettingsPage + settings/{ModelSection,EmbeddingSection,ImageGenSection,AppearanceSection}
        └── DesignSystemPage        # dev-only 像素 QA
```

## 1. 设计 token 与全局 CSS（src/styles/index.css）

```css
@import "tailwindcss";
@plugin "tailwindcss-animate";

@custom-variant dark (&:where(.dark, .dark *));
@custom-variant touch (@media (pointer: coarse));
@custom-variant tablet-landscape (@media (min-width:768px) and (max-width:1366px) and (orientation:landscape));
@custom-variant desktop (@media (min-width:1025px));

@theme {
  --font-sans: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB",
               "Microsoft YaHei", "Helvetica Neue", Helvetica, Arial, sans-serif;
  --font-mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
  --color-primary-500: #6366f1;  --color-primary-600: #5558e6;
  --color-brand-cyan: #61d0e2;  --color-brand-purple: #492497;  --color-brand-pink: #d13870;
  --color-accent-orange: #f97316;
  --radius-sm:.25rem; --radius-md:.375rem; --radius-lg:.5rem;
  --radius-xl:.75rem; --radius-2xl:1rem;  --radius-3xl:1.5rem;
  --blur-sm:8px; --blur-md:12px; --blur-xl:24px;
  --default-transition-duration:.15s;
  --default-transition-timing-function:cubic-bezier(.4,0,.2,1);
  --ease-smooth: cubic-bezier(.25,.1,.25,1);
  --animate-shimmer: shimmer 1.6s infinite;
  --animate-glow: glow 1.5s ease-in-out infinite alternate;
  --animate-pulse-glow: pulse-glow 2s infinite;
  --animate-badge-shimmer: badge-shimmer 3s linear infinite;
  --animate-fade-in-scale: fadeInScale .3s ease-out both;
  --animate-float: float 3s ease-in-out infinite;
}

:root{
  --app-header-h:56px;
  --z-hide:-1; --z-base:0; --z-raised:10; --z-sticky:1000; --z-header:1100;
  --z-fab:1200; --z-dropdown:1300; --z-drawer:1400;
  --z-modal:101500; --z-popover:101600; --z-toast:101900; --z-tooltip:102000;
  --theme-transition-duration:.3s;
}

@layer base {
  body{ -webkit-font-smoothing:antialiased; font-feature-settings:"rlig" 1,"calt" 1;
    transition-property:color,background-color,border-color; transition-duration:var(--theme-transition-duration); }
  .dark{ color-scheme:dark }
  ::-webkit-scrollbar{width:10px;height:10px}
  ::-webkit-scrollbar-track{background:0 0}
  ::-webkit-scrollbar-thumb{background-color:#9ca3af80;background-clip:content-box;
    border:3px solid #0000;border-radius:99px}
  ::-webkit-scrollbar-thumb:hover{background-color:#9ca3afcc}
  .dark ::-webkit-scrollbar-thumb{background-color:#4b556380}
  .scrollbar-thin::-webkit-scrollbar{width:6px;height:6px}
}

@utility glass-effect { backdrop-filter:blur(16px) saturate(180%); background-color:#ffffffd9;
  border:1px solid #ffffff20; .dark &{background-color:#111928d9} }
@utility glass-card { backdrop-filter:blur(10px); background:#ffffff1a; border:1px solid #fff3;
  border-radius:1rem; .dark &{background:#0003;border-color:#ffffff1a} }
@utility glass-morphism { background:rgba(255,255,255,.1); backdrop-filter:blur(10px);
  border:1px solid rgba(255,255,255,.2); box-shadow:0 8px 32px rgba(31,38,135,.37) }

@keyframes shimmer{0%{transform:translateX(-100%)}100%{transform:translateX(100%)}}
@keyframes glow{0%{box-shadow:0 0 5px rgba(59,130,246,.5)}to{box-shadow:0 0 20px rgba(59,130,246,.8),0 0 30px rgba(59,130,246,.6)}}
@keyframes pulse-glow{0%,to{box-shadow:0 0 #3b82f666}50%{box-shadow:0 0 0 10px #3b82f600}}
@keyframes badge-shimmer{0%{background-position:200% 0}to{background-position:-200% 0}}
@keyframes fadeInScale{0%{opacity:0;transform:scale(.8)}60%{opacity:1;transform:scale(1.05)}to{opacity:1;transform:scale(1)}}
@keyframes float{0%,to{transform:translateY(0)}50%{transform:translateY(-10px)}}
```

index.html body（参考站原版页面渐变）：
```html
<body class="bg-gradient-to-br from-slate-50 via-blue-50/30 to-indigo-50/50
             dark:from-gray-900 dark:via-gray-800/30 dark:to-slate-900/50
             text-gray-900 dark:text-gray-100 antialiased">
```

### 1.1 暗色模式（class + localStorage + auto 18:00–06:00）

index.html head 无闪烁内联脚本：
```html
<script>(function(){try{
  var t=localStorage.getItem('pa-theme')||'system',d;
  if(t==='dark')d=true;else if(t==='light')d=false;
  else if(t==='auto'){var h=new Date().getHours();d=(h>=18||h<6);}
  else d=matchMedia('(prefers-color-scheme: dark)').matches;
  var e=document.documentElement;e.classList.add(d?'dark':'light');e.dataset.theme=d?'dark':'light';
}catch(e){}})();</script>
```
lib/theme.ts：`ThemeMode='light'|'dark'|'system'|'auto'`；applyTheme 换 class + data-theme；system 订阅 matchMedia change；auto 用 setTimeout 到下一个 18:00/06:00 边界重估。

## 2. 组件规格（精确到类名）

### 2.1 AppHeader
- `motion.header` — `sticky top-0 z-[var(--z-header)] w-full bg-white/60 dark:bg-gray-900/60 backdrop-blur-md border-b border-gray-200/50 dark:border-gray-700/50`
- 内行 `w-full px-4 sm:px-10 h-[var(--app-header-h,56px)] flex items-center justify-between`
- 自动隐藏：主滚动容器 scrollY 下滑 >80px 隐藏、任何上滑或到顶显示；`animate={{y: hidden?'-100%':0}}` `transition={{duration:.3, ease:[.25,.1,.25,1]}}`；顶部不可见 10px 悬停带唤回
- 左：logo `w-6 sm:w-8` + 字标「引途医疗专利智能体」`text-base sm:text-xl font-bold text-gray-900 dark:text-white`（链接 `/`）
- 中（`hidden md:flex absolute left-1/2 -translate-x-1/2 gap-12`）：工作台(`/`)·案例库(`/oa/cases`)·设置(`/settings`)；`text-[15px] font-medium`，active `text-gray-900 dark:text-white`；下划线 `absolute -bottom-1 left-0 h-0.5 bg-gradient-to-r from-blue-500 to-purple-500 w-0→w-full transition-all duration-300`
- 右（`gap-4`）：ThemeToggle 等 `w-8 h-8 rounded-full hover:bg-gray-100 dark:hover:bg-gray-800`
- 下拉面板：`w-64 bg-white dark:bg-gray-800 rounded-xl shadow-xl border border-gray-200 dark:border-gray-700 py-1.5 z-[60]`；motion `{opacity:0,y:10,scale:.95}→{1,0,1}` .2s + AnimatePresence

### 2.2 Sidebar
- `fixed top-[var(--app-header-h)] left-0 h-[calc(100vh-var(--app-header-h))] bg-gray-50 dark:bg-gray-900 border-r border-gray-100 dark:border-gray-800 z-40 flex flex-col overflow-hidden`，motion 宽度 260⇄72（桌面）/200⇄72（平板横屏）`.3s ease-smooth`；主内容 padding-left 同步动画
- 折叠持久化 `pa-sidebar-collapsed` / 分组折叠 `pa-sidebar-collapsed-groups`
- 顶块 p-3：**新建会话**（展开 `w-full h-9 gap-2 px-3 rounded-xl bg-white dark:bg-gray-800 border shadow-sm text-sm font-medium hover:bg-gray-100 active:scale-[0.99]` + PlusIcon w-4；折叠 icon-only `w-10 h-10 mx-auto`）；**搜索会话** `h-9 rounded-lg pl-8 text-[13px]` + MagnifyingGlassIcon 绝对定位
- 分组（`flex-1 overflow-y-auto scrollbar-thin px-2`）四组：交底书/论文转专利/专利解读/审查答复；组头 `px-2 py-1.5 text-xs font-medium text-gray-500` + Chevron 旋转 + 计数徽章 `text-[10px] min-w-[16px] rounded-full px-1 bg-gray-200 dark:bg-gray-700`；条目 `px-2 py-2 rounded-lg text-[13px] hover:bg-gray-100`，active `bg-gray-200/70 font-medium`；**待确认红点** `min-w-[14px] h-3.5 rounded-full bg-red-500 text-white text-[9px] font-bold ring-2 ring-white dark:ring-gray-900`；hover 显示 … 菜单（重命名/删除）
- 空态卡 `mt-4 mx-1 px-3 py-3.5 rounded-xl border border-[#492497]/20 text-[13px] text-gray-600`
- 底块 p-3 border-t：设置行 + 折叠钮（ChevronDoubleLeftIcon 旋转 180）
- MobileDrawer（<768px）：scrim `fixed inset-0 z-[55] bg-black/40 backdrop-blur-sm` + 面板 `w-[280px] x:'-100%'→0 .3s`

### 2.3 Composer（招牌组件）
Props: `variant:'hero'|'chat'`, `accent:'indigo'|'orange'`, `placeholder`, `onSend`, `busy`。
- 外壳(hero)：`relative w-full max-w-[800px] mx-auto flex flex-col items-center gap-3 sm:gap-4 mt-0 rounded-3xl p-3 sm:p-4 border-2 border-transparent transition-all duration-200`；`header` 插槽渲染在内卡上方（首页 = 渐变胶囊 + logo/标题组，见 §3.1 实测：外壳包含标题）；拖放 `border-2 border-dashed border-blue-500 bg-blue-50/50 ring-4 ring-blue-500/10` + DragOverlay `absolute inset-0 z-20 rounded-3xl backdrop-blur-sm` 居中 ArrowUpTrayIcon + 「拖放文件到此处上传」
- 内卡（**Playwright 实测**：idle 1px，聚焦才 2px）：`w-full rounded-2xl bg-white dark:bg-gray-800 shadow-sm relative transition-all duration-300`；idle `p-4 border border-gray-200 dark:border-gray-700`；focus `p-[15px] border-2 border-[#6366f1] dark:bg-gray-900 shadow-[0_0_12px_rgba(99,102,241,0.15)]`；orange `border-orange-500 shadow-[0_0_12px_rgba(249,115,22,0.15)]`。`p-4→p-[15px]` 抵消 1px 描边增量，border-box 下聚焦零跳动
- 文件 pills：`grid grid-cols-1 sm:grid-cols-3 gap-3 mb-3`；FilePill `gap-2 bg-gray-100 dark:bg-gray-800 p-2 rounded-lg border`；图标块 `w-8 h-8 rounded bg-gray-200`（按类型 Heroicon/缩略图）；名 `text-xs font-medium truncate` + 大小 `text-[10px] text-gray-500`；进度条 `h-1 mt-1 rounded-full` 内 `bg-[#6366f1]`；错误 border-red-300+重试；移除钮 `-top-1.5 -right-1.5 w-4 h-4 rounded-full bg-gray-500 text-white opacity-0 group-hover:opacity-100`
- 输入：contentEditable `text-base leading-relaxed min-h-[1.75rem] max-h-[200px] overflow-y-auto outline-none scrollbar-thin`；placeholder 用 `empty:before:content-[attr(data-placeholder)] before:text-gray-400`；粘贴纯文本；Enter 发送 Shift+Enter 换行；IME 安全（isComposing）
- 工具栏 `flex items-center justify-between gap-2 mt-4`：左 `toolbarLeft` 插槽（首页 = ModuleToggle，`hidden sm:flex`；sm 以下折到输入框上方 `flex sm:hidden … overflow-x-auto`），右 `+`/发送同组 `flex items-center gap-2 ml-auto`；无 `toolbarLeft`（chat 变体）时退回 `+` 左 / 发送右。PlusMenu 钮 `w-8 h-8 rounded-full`（open `bg-gray-200 rotate-45`）；菜单 `bottom-full mb-2 left-0 w-56 rounded-xl shadow-lg border py-1.5` animate-in fade-in slide-in-from-bottom-2；SendButton `w-9 h-9 rounded-full`——disabled 灰/ready `bg-[#6366f1] shadow-lg shadow-indigo-500/30`（橙变体）/busy 显 StopIcon
- chat 变体：sticky bottom-0，`max-w-[800px] px-4 pb-3` + 免责声明 `text-center text-xs text-gray-400 mt-2`「内容由 AI 生成，请核对后使用」

### 2.4 SegmentedToggle
轨道 `relative flex items-center gap-1 bg-gray-100 dark:bg-gray-800 rounded-full p-0.5 h-9`（内卡工具栏内加 `shrink-0`）；thumb=motion layoutId `absolute top-0.5 bottom-0.5 rounded-full bg-white dark:bg-gray-600 shadow-sm` `.3s ease-smooth`；段（**Playwright 实测**）`relative flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-medium transition-colors duration-200`，active `text-gray-900 dark:text-white` / inactive `text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300`，可选 `icon`（w-3.5）。首页 ModuleToggle 四段位于 Composer 内卡工具栏左侧，用简称 + 图标：交底书/论文转专利/专利解读/审查答复（全称仍见侧栏与 zh.home.modules）；accent 映射 paper=orange 其余 indigo。

### 2.5 FeatureChips + 悬停预览
行 `w-full flex flex-wrap gap-2 justify-center`，motion stagger delay i*.05；chip `gap-1.5 px-3 py-1.5 rounded-full border bg-white dark:bg-gray-800 text-xs font-medium hover:border-[#6366f1]/40 hover:shadow-sm` + icon w-3.5；预览卡（300ms 延迟）`absolute top-full mt-2 max-w-[300px] sm:max-w-[340px] rounded-xl border bg-white shadow-sm` animate-in fade-in zoom-in-95，体 `px-3.5 py-3` 标题 text-sm font-semibold + 描述 text-xs text-gray-500。
chip 集：交底书[上传项目材料/挖掘专利点/联网查新/生成交底书/示例案件]；论文[上传论文PDF/直接生成/人工确认模式/附图预览]；解读[输入公开号/上传专利PDF/权利要求树/通俗解读报告]；答复[上传审查意见/答复策略/案例库检索]。

### 2.6 通用 UI
- Card `rounded-2xl border border-gray-200/60 dark:border-gray-700/60 bg-white dark:bg-gray-800 shadow-sm`；accent 变体 `border-2 border-indigo-300 shadow-lg`
- Button：primary `bg-[#6366f1] hover:bg-[#5558e6] text-white rounded-xl px-4 py-2 text-sm font-medium shadow-sm`；gradient `bg-gradient-to-r from-[#61d0e2] via-[#492497] to-[#d13870] text-white rounded-full px-5 py-2 font-semibold shadow-lg shadow-indigo-500/30 hover:scale-105 hover:shadow-xl duration-300 active:scale-[0.99]`；secondary/ghost/danger
- Modal：overlay `bg-black/50 backdrop-blur-sm animate-in fade-in`；panel `rounded-2xl shadow-xl max-w-lg p-6 animate-in fade-in zoom-in-95 duration-200`；焦点陷阱+Esc
- Drawer：右侧 `w-full sm:w-[480px] shadow-xl border-l` slide-in-from-right duration-300 + scrim
- Toast：右下 `z-[var(--z-toast)]`；`rounded-xl border shadow-xl px-4 py-3 text-sm animate-in slide-in-from-bottom-2`；图标 emerald/amber/red w-5；4s 自动消失
- ToggleSwitch：`h-5 w-9 rounded-full`，on `bg-[#6366f1]`，knob `h-4 w-4 bg-white shadow translate-x-0.5→[18px]`
- ThemeToggle：下拉 w-64 四行 浅色/深色/跟随系统/自动（18:00–06:00 深色），active 行 CheckIcon `text-[#6366f1]`
- Skeleton：`bg-gray-200/80 dark:bg-gray-700/60 animate-pulse`；shimmer 变体 after 渐变扫过
- EmptyState：图标砖 `w-14 h-14 rounded-2xl bg-gray-100` + 标题 text-sm + 描述 text-xs + 可选 CTA
- Badge：`text-[10px] font-semibold px-1.5 py-0.5 rounded`；gradient 变体 blue-500→purple-500
- Input/Select：`h-10 rounded-xl border border-gray-300 dark:border-gray-600 px-3.5 text-sm focus:border-[#6366f1] focus:ring-4 focus:ring-indigo-500/10`

### 2.7 StreamingMarkdown
- 按 `\n\n`（码块围栏外）切稳定块，除尾块外 memo 渲染（react-markdown 全家桶）；流式中尾部闪烁光标 `w-[2px] h-[1.1em] bg-gray-400 animate-pulse`
- typography：`prose prose-sm sm:prose-base dark:prose-invert max-w-none` + 覆盖（headings font-semibold tracking-tight；code 无引号背景灰；表格描边）
- MermaidBlock：仅围栏闭合后渲染（未闭合尾块显 `h-40` shimmer +「图表生成中…」）；`mermaid.initialize({startOnLoad:false, theme:isDark?'dark':'default', securityLevel:'strict'})`；SVG 注入 `bg-white dark:bg-gray-900 rounded-xl border p-4 overflow-x-auto`；解析错误→amber「图表语法待修正」+折叠源码；主题切换全量重渲
- CodeBlock：`rounded-xl bg-gray-900 text-gray-100 text-[13px] p-4 overflow-x-auto` + 复制钮

### 2.8 StepProgress
sticky 横幅 `sticky top-0 z-30 border-b border-blue-100 bg-gradient-to-r from-blue-50 to-indigo-50 dark:from-gray-800/80 dark:to-gray-800/80 px-4 py-1`，内 `max-w-[800px] mx-auto flex items-center h-9` 横向滚动；步骤圆 `w-6 h-6 rounded-full text-[10px] font-semibold`——done `bg-[#6366f1] text-white`(CheckIcon)/current 白底 `border-2 border-[#6366f1] text-[#6366f1] animate-pulse-glow`/pending 灰；标签 `text-xs ml-1.5 mr-3`；连接线 `h-0.5 w-6 rounded`（过线 #6366f1）。移动端「当前步骤 3/8 · 专利点挖掘」紧凑文本。
步骤集：交底书[边界确认→材料扫描→专利点挖掘→联网查新→预览确认→生成全文→自检→交付]；论文[上传论文→模式选择→生成五大部分→附图→交付]；解读[获取文本→解析→生成报告→交付]；答复[上传通知书→问题解析→策略选择→草拟答复→交付]。

### 2.9 Dropzone
`rounded-2xl border-2 border-dashed border-gray-300 bg-gray-50/50 px-6 py-10 flex flex-col items-center gap-3 hover:border-[#6366f1]/50 hover:bg-blue-50/30`；drag-over `border-blue-500 bg-blue-50/50 ring-4 ring-blue-500/10`；ArrowUpTrayIcon w-8「点击或拖放文件到此处」+ 类型/大小提示。

### 2.10 VersionHistory + DownloadMenu
行 `flex justify-between rounded-xl border px-4 py-3 hover:bg-gray-50`；左 V 徽章(gradient)+文件名 text-sm truncate+时间戳 text-xs（`2026-08-25 14:32 · 纠正迭代`；标签=初稿/合并迭代/纠正迭代）；最新行「当前」emerald 徽章；右 MD/DOCX/PDF chip 钮。位于 DocumentPanel 头部 ClockIcon 下拉（w-72）与 DeliveryCard。

### 2.11 ClaimTree
递归；节点 `rounded-lg border px-3 py-2 text-sm`——独权 `border-indigo-200 bg-indigo-50/50 dark:bg-indigo-500/10 font-medium`，从权白底；头行 `权利要求 {n}` + 类型徽章 + 一行白话增量 truncate；子级 `ml-4 pl-4 border-l space-y-2 mt-2`；点击展开全文（AnimatePresence height）；多引（「权1或2」）渲染重复链接 chips。

## 3. 路由与页面

```
/                      → AppLayout → HomePage
/disclosure/:id  /paper/:id  /reader/:id  /oa/:id   → 工作台
/oa/cases              → SecondaryLayout → OACasesPage
/settings              → SecondaryLayout → SettingsPage
/design-system         → dev-only
```
AppLayout：`h-screen bg-white dark:bg-gray-900 flex flex-col overflow-hidden` → header → `flex flex-1 overflow-hidden relative` → Sidebar + `<main class="flex-1 overflow-y-auto flex flex-col">`（即 header 自动隐藏监听的滚动容器）。

### 3.1 HomePage（参考站 hero 克隆 —— **Playwright 实测**，非 CSS bundle 推断）

以下结构与坐标为 Playwright 抓取 `https://www.fojiaoai.cn/dashboard/` 真实 DOM + 计算样式所得（1440×900，侧栏 260 展开态），已取代此前基于 CSS bundle 的推断描述：

```
main.flex-1.overflow-y-auto.flex.flex-col                      (x=260,y=57,w=1180,h=843)
└── div "p-4 pb-20 flex flex-col flex-1 pt-[10vh]"             ← 顶部定位，**不是** justify-center
    └── div 拖放外壳 "w-full max-w-[800px] mx-auto flex flex-col items-center
             gap-3 sm:gap-4 mt-0 relative transition-all duration-200 rounded-3xl p-3 sm:p-4"
        │                                                       (x=450,y=147,w=800)  ← 外壳**包含标题**
        ├── input.hidden ×2                                     ← 隐藏 file input
        ├── div "flex flex-col items-center gap-1 mb-1 sm:mb-2" (x=632,y=165,h=112)
        │   ├── div "flex justify-center w-full mb-1 sm:mb-2"   (y=165,h=36)  ← 渐变胶囊行（胶囊高 36）
        │   └── div "flex items-center justify-center gap-2 sm:gap-3" (y=213,h=64) ← **logo 与标题同一行**
        └── div "w-full"                                        ← composer 内卡外层
            └── div "w-full border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800
                     rounded-2xl p-4 shadow-sm relative transition-all duration-300"
                                                                (x=468,y=301,w=764,h=114, radius 16px)
                ├── 输入区 [contenteditable]                    (x=485,y=318,w=730,h≈26)
                └── 工具栏行 "flex items-center justify-between mt-4"  (y≈362,h=36)
                    ├── 左：分段切换器轨道 "flex items-center gap-1 bg-gray-100 dark:bg-gray-800
                    │        rounded-full p-0.5 h-9 shrink-0 relative z-10"（段 y=366,h=28）
                    │   段钮 "relative flex items-center gap-1.5 px-3 py-1.5 rounded-full
                    │        text-xs font-medium transition-colors duration-200"
                    │   active `text-gray-900` / inactive `text-gray-500 dark:text-gray-400
                    │        hover:text-gray-700 dark:hover:text-gray-300`
                    └── 右：+ 按钮、（参考站另有麦克风钮，本项目未采用）、圆形发送钮
    └── FeatureChips 行（外壳之外）                              (y≈447)
```

关键约束（与参考站逐项对齐）：
- **垂直定位**：容器 `pt-[10vh]`（1440×900 下 = 90px），内容顶部对齐而非垂直居中；胶囊 y=165 / 标题行 y=213 / composer 内卡 y=301 / chips y≈447。
- **logo 与主标题同一行**：logo `w-12 h-12 sm:w-16 sm:h-16 object-contain` + 标题 `text-2xl sm:text-3xl md:text-4xl font-bold tracking-tight`（AnimatePresence fade y:6，按模块变文案：交底书「准备好撰写交底书了吗？」/论文「把论文变成专利申请」/解读「读懂任何一件专利」/答复「从容答复审查意见」）+ Beta 渐变角标绝对定位于标题右上。
- **ModuleToggle 在 composer 内卡工具栏左侧**（不是 composer 上方独立一行），与右侧 +/发送钮同一行。本项目 4 模块（参考站仅 2 段）故段标签用简称 `zh.home.modulesShort`（交底书/论文转专利/专利解读/审查答复）+ Heroicon（`gap-1.5`），轨道 `shrink-0`；实测轨宽 384px，内卡可用 730px，余量充足。sm 以下折行到输入框上方（`flex sm:hidden` + `overflow-x-auto`），对应参考站的移动端备用行。
- **拖放外壳包含标题**：`rounded-3xl p-3 sm:p-4` + `border-2 border-transparent`（实测内卡相对外壳偏移 18px = p-4 16 + 边框 2），拖放态虚线/ring/blur 覆盖层（`rounded-3xl`）覆盖胶囊+标题+composer 整体，拖文件到标题区也触发上传高亮。
- **内卡描边**：idle 1px `border border-gray-200 dark:border-gray-700` + `transition-all duration-300`；聚焦才 `border-2` + accent 色 + `shadow-[0_0_12px_rgba(99,102,241,.15)]`（橙变体 `rgba(249,115,22,.15)`）。聚焦时 `p-4 → p-[15px]` 抵消 1px 描边增量，border-box 下内容零跳动。
- 切模块联动：placeholder / hero 标题文案 / accent 色（论文转专利 = 橙）/ FeatureChips 组。

提交→`POST /cases {module, title:前20字}`→上传附件→navigate 工作台。

### 3.2 DisclosurePage（旗舰工作台样板）
左流式列 `flex-1 min-w-0 flex flex-col relative`：StepProgress → 滚动流 `max-w-[800px] mx-auto px-4 py-6 space-y-4` → chat Composer。xl 以上右 DocumentPanel `w-[46%] max-w-[720px] border-l`：头 `h-12 px-4` 标题+V 徽章+流式进度点+版本下拉+下载+全屏+收起；流式中头下 `h-0.5 bg-gradient-to-r from-blue-500 to-purple-500 animate-badge-shimmer bg-[length:200%_100%]`；体 `px-8 py-6` StreamingMarkdown。xl 以下为流内 DocumentCard（max-h-[420px]+渐隐+展开全文 Modal）。
流条目：user 右对齐气泡 `self-end max-w-[85%] bg-gray-100 dark:bg-gray-800 rounded-2xl px-4 py-2.5 text-[15px]`（+附件 mini pills）；assistant 全宽 markdown；stage 卡片；notice 居中 text-xs 灰。
卡序：IntakeForm→MaterialUpload→TypeSuggest(条件)→PatentPoints→PriorArt→PreviewConfirm→(生成流入 DocumentPanel)→SelfCheck→Delivery→ClaimBias。迭代经 chat composer；每轮结束推新版本+Delivery 更新+「## 合并摘要（留档）」assistant 消息。
骨架：横幅 h-9 + 三张 h-24 shimmer 卡 + composer h-28。空会话：流列内 mini-hero。

### 3.3 PaperPage
同壳（5 步、橙 accent）。MaterialUpload(单 PDF)→ModeSelectCard：两 tile `grid sm:grid-cols-2 gap-3`，`rounded-xl border-2 p-4 hover:border-orange-400/50`，选中 `border-orange-500 bg-orange-50/50`——直接生成/人工确认模式→生成五大部分流入 DocumentPanel→FiguresPreviewCard：`grid grid-cols-2 sm:grid-cols-3 gap-3`，tile `rounded-xl border p-2`+图区 `aspect-[3/4] bg-gray-50 rounded-lg object-contain`+图注 text-xs；点击 lightbox Modal max-w-3xl+下载→Delivery(DOCX/PDF)。

### 3.4 ReaderPage
输入卡：公开号 Input(placeholder `CN202410XXXXXX.X 或 CN117XXXXXXA`)+开始解读；分隔「或」；Dropzone(pdf)。解析进度只读列表→报告流。xl 上 DocumentPanel 为主面：ReportToc 侧栏 `w-44 text-[13px]`（active `bg-indigo-50 text-[#6366f1]`，scroll-spy）+ ClaimTree 交互渲染。头部导出 MD。composer 可追问。

### 3.5 OAPage
MaterialUpload(通知书 PDF)→解析进度→OAIssuesCard：「共识别 {n} 个审查问题」；行 `rounded-xl border p-3`：序号+条款红徽章 `bg-red-50 text-red-600`（如 专利法22条3款·创造性）+涉及权利要求 chips+摘录 line-clamp-2+相似案例 mini 列表→OAStrategyCard：四选项 tiles（仅意见陈述/修改权利要求/修改说明书/补正）+逐 issue 覆写 accordion+「生成答复草稿」→草稿流入 DocumentPanel→Delivery+「存入案例库」（confirm Modal 带 frontmatter 字段）。

### 3.6 OACasesPage（SecondaryLayout）
页头 `px-4 sm:px-8 py-4 bg-white border-b shadow-sm`（标题+导入案例）。体 `max-w-4xl mx-auto p-4 sm:p-8 space-y-4`：工具栏（语义检索 Input+「语义检索」徽章+清除；结案结果/缺陷类型过滤）；案例行（Card p-4：标题+结果徽章 授权emerald/驳回red/审中amber+标签 chips+日期；检索态相似度徽章）；点击→Drawer 详情。空态/骨架齐备。

### 3.7 SettingsPage
aside `w-64 bg-white dark:bg-gray-800 border-r`：模型服务(CpuChipIcon)/向量与检索(CircleStackIcon)/图像生成(PhotoIcon)/外观(PaintBrushIcon)；active `bg-indigo-50 text-[#6366f1]`。移动端横向 pill tabs。内容 `max-w-2xl space-y-6` 每节 Card p-6：
- ModelSection：Base URL/API Key(password+眼睛)/模型名(datalist)；测试连接（loading Spinner→emerald「连接成功 · 812ms」/red 失败+错误）+保存
- EmbeddingSection：启用开关+provider Select(智谱/DashScope/MiniMax/本地/自定义)+base/key/model/维度+测试+重建索引(danger,confirm Modal)
- ImageGenSection：启用+provider+base/key/model+测试出图(缩略图 w-24)
- AppearanceSection：主题四 radio tiles+语言 Select(简体中文,禁用)

### 3.8 DesignSystemPage（dev-only）
全组件全状态网格（含强制 hover/focus/disabled/streaming/暗色并排）+token 色板——像素 QA 台。

## 4. HITL StageCard 模式（一次设计全平台复用）

```ts
type StreamItem =
  | { kind:'user';      id; text; files?: FileMeta[] }
  | { kind:'assistant'; id; markdown; streaming }
  | { kind:'stage';     id; stage: Stage }
  | { kind:'doc_ref';   id; docId }
  | { kind:'notice';    id; text };
type Stage = { id; type: StageType; status:'active'|'completed'|'skipped';
               payload: unknown; result?: unknown };
type StageType = 'intake'|'type_suggest'|'material_upload'|'patent_points'|'prior_art'
               | 'preview_confirm'|'self_check'|'claim_bias'|'mode_select'
               | 'oa_issues'|'oa_strategy'|'figures_preview'|'delivery';
```
StageCardShell：active=`border-2 border-indigo-200 dark:border-indigo-500/40` + 头条 `px-4 py-2.5 bg-indigo-50/50`（图标块 `w-6 h-6 rounded-lg bg-[#6366f1] text-white`+标题+「待确认」amber pulse 徽章）+体 p-4+底 `justify-end gap-2 px-4 py-3 bg-gray-50/50`（跳过 ghost+确认 primary）；completed 折叠为一行摘要（✓emerald+「已确认边界：发明 · 一种…」+Chevron 可重展只读）；skipped 灰。registry `Record<StageType, FC>`；未知类型渲通用 JSON 卡。提交：`POST /cases/:id/pipeline/input {step_key, payload}` → 乐观 completed → 后端 SSE 续推。仅最新 stage 可 active；侧栏红点=有 active stage。

## 5. 状态与数据

- TanStack Query：useSessions/useSession(快照)/useVersions/useSettings/useCases；变更走 mutation
- zustand：uiStore（主题/侧栏/抽屉/toasts/header hidden）；sessionStore（items/docs{markdown,streaming,versionId}/pipeline/connection）；composerStore（草稿+附件进度）
- SSE（lib/sse.ts）：fetch+ReadableStream 手解（支持 header/abort）`GET /cases/:id/events?after=<cursor>`；缓冲→按 `\n\n` 切→parse event/data/id→dispatch。命令=普通 POST 返回 202，一切输出走通道。delta 入 ref 缓冲经 rAF ~30fps 刷入；吸底滚动（上滑 >120px 显「回到底部」FAB）；重连 1s→8s 退避 5 次带 after=cursor，放弃则错误卡+重试；重连成功先拉快照再续
- 事件表（后端 canonical）：step_status / llm_delta{step_key,channel:'chat'|'doc',text} / llm_done / doc_version / interaction_required / search_progress / artifact_created / case_title / log / error{retryable} / pipeline_done / ping
- 上传：XHR onprogress；超限/类型拒→Toast
- **Mock 模式**：`VITE_USE_MOCKS=1` 换 transport 为 mocks/mockServer.ts，回放脚本化时间线（完整交底书含 mermaid+KaTeX+全卡片类型/论文/解读/OA 各一）——前端可先于后端完整开发与像素 QA

## 6. 里程碑

M1 脚手架+token+壳（§1 全量+主题+i18n+ui 原语+AppLayout/Header/Sidebar+路由占位）→ M2 Home hero 全套+ThemeToggle → M3 流式设施（sse/stores/mock+StreamingMarkdown+DocumentPanel）→ M4 Pipeline+交底书全卡片+StepProgress+chat composer+版本下载（mock 全流程）→ M5 其余模块页 → M6 设置页 → M7 打磨+像素全检。

## 7. 像素验收协议

参考站与本地并排（1440×900/1024×768/390×844，双主题）；DevTools computed-styles 对照+50% 透明截图叠加。核对表（每行须精确一致）：

| 检查 | 期望 |
|---|---|
| 头部高/背景/blur/描边 | 56px · rgba(255,255,255,.6) · blur(12px) · 1px rgba(229,231,235,.5) |
| 头部自动隐藏 | translateY(-100%), .3s cubic-bezier(.25,.1,.25,1)；10px 悬停带唤回 |
| 导航下划线 | h-0.5，#3b82f6→#a855f7，hover/active w 0→100% |
| 侧栏宽/动画 | 260⇄72 桌面、200⇄72 平板横屏、280 移动抽屉；.3s |
| 侧栏面 | #f9fafb 亮/#111827 暗；边 #f3f4f6/#1f2937 |
| Composer 外壳 | max-w 800px，rounded-3xl(24px)，p 12→16px |
| Composer 聚焦 | 2px #6366f1 + 0 0 12px rgba(99,102,241,.15)；橙变体 rgba(249,115,22,.15) |
| 拖放态 | 虚线 blue-500+bg blue-50/50+ring-4 blue-500/10+blur 覆盖 |
| 发送钮 | 36px 圆；激活 #6366f1+shadow-lg shadow-indigo-500/30 |
| 分段切换 | 轨 h-36px gray-100 p-2px；thumb .3s cubic-bezier(.25,.1,.25,1) |
| Chips/预览卡 | rounded-full text-xs；卡 ≤340px rounded-xl shadow-sm px-14/py-12 |
| 渐变胶囊 | #61d0e2→#492497→#d13870，hover scale 1.05+shadow-xl |
| 卡/下拉/弹窗 | rounded-2xl shadow-sm / rounded-xl shadow-xl w-56–72 / bg-black/50 blur |
| 开关 | 20×36px，on #6366f1，knob 16px 白 |
| 滚动条 | 10px 胶囊 #9ca3af80（暗 #4b556380）透明轨 |
| body 渐变 | slate-50→blue-50/30→indigo-50/50；暗 gray-900→gray-800/30→slate-900/50 |
| 暗色切换 | .dark 全面翻转 gray-900/800；color-scheme:dark；.3s 过渡 |
| 字号阶梯 | 导航 15px、侧栏 13px、徽章 10/9px、hero 24→30→36px bold |

功能 QA：IME 回车、mermaid 换肤重渲、SSE 重连+刷新续传、下载、localStorage 持久化。
