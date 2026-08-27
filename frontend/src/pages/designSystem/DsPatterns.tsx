import {
  BookOpenIcon,
  CircleStackIcon,
  DocumentTextIcon,
  PhotoIcon,
} from '@heroicons/react/24/outline'
import { zh } from '../../i18n/zh'
import { Card } from '../../components/ui/Card'
import { EmptyState } from '../../components/ui/EmptyState'
import { Skeleton } from '../../components/ui/Skeleton'
import { StepProgress, type DisplayStep } from '../../components/pipeline/StepProgress'
import { StreamItemView } from '../../components/pipeline/StreamItemView'
import { VersionHistory, type VersionEntry } from '../../components/document/VersionHistory'
import { emptySession, type CaseSessionState } from '../../stores/sessionStore'
import type { StreamItem } from '../../types/stream'

const STEPS: DisplayStep[] = [
  { key: 'a', label: '边界确认', status: 'done' },
  { key: 'b', label: '材料扫描', status: 'done' },
  { key: 'c', label: '专利点挖掘', status: 'current' },
  { key: 'd', label: '联网查新', status: 'pending' },
  { key: 'e', label: '交付', status: 'pending' },
]

const STEPS_FAILED: DisplayStep[] = [
  { key: 'a', label: '边界确认', status: 'done' },
  { key: 'b', label: '材料扫描', status: 'done' },
  { key: 'c', label: '联网查新', status: 'failed' },
  { key: 'd', label: '生成全文', status: 'pending' },
  { key: 'e', label: '交付', status: 'pending' },
]

const VERSIONS: VersionEntry[] = [
  {
    id: 'v3',
    version: 3,
    filename: '技术交底书_便携式术后康复监测装置.md',
    createdAt: '2026-08-25 14:32:10',
    iterationType: 'correction',
  },
  {
    id: 'v2',
    version: 2,
    filename: '技术交底书_便携式术后康复监测装置.md',
    createdAt: '2026-08-25 11:07:45',
    iterationType: 'merge',
  },
  {
    id: 'v1',
    version: 1,
    filename: '技术交底书_便携式术后康复监测装置.md',
    createdAt: '2026-08-25 09:51:02',
    iterationType: 'initial',
  },
]

const RICH_MARKDOWN = `## 富文本溢出 QA

窄屏下下面三块都必须**在自己的容器内横向滚动**，不得撑破页面。

| 权利要求 | 类型 | 引用 | 增量特征（长文本用于逼出横向滚动） | 备注 |
|---|---|---|---|---|
| 1 | 独立权利要求 | — | 一种可穿戴式术后康复监测装置，包括柔性基底、设于所述柔性基底上的多通道生物电极阵列 | 主权 |
| 2 | 从属权利要求 | 权 1 | 根据权利要求 1 所述的装置，其特征在于所述多通道生物电极阵列包括至少六个干电极 | — |
| 3 | 从属权利要求 | 权 1 或 2 | 根据权利要求 1 或 2 所述的装置，其特征在于还包括与所述电极阵列电连接的模数转换模块 | 多引 |

\`\`\`mermaid
flowchart LR
  A[柔性基底] --> B[多通道电极阵列]
  B --> C[模数转换模块]
  C --> D[边缘计算单元]
  D --> E[无线收发模块]
  E --> F[移动端 App]
\`\`\`

行内公式 $R_{sk}=\\rho L / A$，块级公式：

$$
\\mathrm{SNR} = 10\\log_{10}\\frac{\\sum_{n=1}^{N} s^2[n]}{\\sum_{n=1}^{N} \\left(x[n]-s[n]\\right)^2}
$$

\`\`\`python
def compute_snr(signal: list[float], noisy: list[float]) -> float:
    """一行很长的代码，用来验证 CodeBlock 的 overflow-x-auto 是否生效而不是把页面撑破。"""
    return 10 * math.log10(sum(s * s for s in signal) / sum((x - s) ** 2 for x, s in zip(noisy, signal)))
\`\`\`
`

function fakeSession(): CaseSessionState {
  return emptySession()
}

const NOTICE_ITEMS: StreamItem[] = [
  { kind: 'notice', id: 'n1', text: '正在检索国知局公开数据…（第 2/5 页）' },
  {
    kind: 'notice',
    id: 'n2',
    text: '国知局检索超时（Playwright 30s 未返回），本轮未取到命中结果。',
    level: 'error',
    retryable: true,
    stepKey: 'prior_art_search',
    stepLabel: '联网查新',
  },
  {
    kind: 'notice',
    id: 'n3',
    text: '模型返回的 JSON 无法解析：第 2 个专利点缺少 "novelty" 字段。',
    level: 'error',
    retryable: false,
    stepKey: 'points_mining',
    stepLabel: '专利点挖掘',
  },
]

const USER_ITEM: StreamItem = {
  kind: 'user',
  id: 'u1',
  text: '把第 3 个专利点的独权改写成方法权利要求。',
  files: [{ id: 'f1', name: '实验数据说明.docx', size: 98304 }],
}

const ASSISTANT_ITEM: StreamItem = {
  kind: 'assistant',
  id: 'a1',
  markdown: '好的，我会把第 3 个专利点改写为方法权项，并同步调整说明书对应段落。',
  streaming: false,
}

function StreamRow({ item }: { item: StreamItem }) {
  return (
    <StreamItemView
      item={item}
      caseId="ds"
      session={fakeSession()}
      onStageSubmit={() => undefined}
      onStageSkip={() => undefined}
      onRetryStep={() => undefined}
    />
  )
}

/**
 * 页面级模式（非单一原语）的全状态展示：
 * StepProgress（含 failed）、流条目四型（user / assistant / notice / 失败卡）、
 * VersionHistory（有数据 / 空）、EmptyState 家族、骨架屏家族、富文本溢出 QA。
 */
export function DsPatterns() {
  return (
    <div className="space-y-8">
      <section className="space-y-3">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-400 dark:text-gray-500">
          StepProgress（正常 / 含失败步骤；&lt;sm 自动切紧凑文本）
        </h3>
        <div className="rounded-xl overflow-hidden border border-gray-200 dark:border-gray-700">
          <StepProgress steps={STEPS} />
        </div>
        <div className="rounded-xl overflow-hidden border border-gray-200 dark:border-gray-700">
          <StepProgress steps={STEPS_FAILED} />
        </div>
      </section>

      <section className="space-y-3">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-400 dark:text-gray-500">
          流条目：user 气泡 / assistant / notice / 失败卡（可重试 · 不可重试）
        </h3>
        <div className="space-y-4">
          <StreamRow item={USER_ITEM} />
          <StreamRow item={ASSISTANT_ITEM} />
          {NOTICE_ITEMS.map((item) => (
            <StreamRow key={item.id} item={item} />
          ))}
        </div>
      </section>

      <section className="space-y-3">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-400 dark:text-gray-500">
          VersionHistory（三版 / 空态）
        </h3>
        <Card className="p-2">
          <VersionHistory versions={VERSIONS} onDownload={() => undefined} />
        </Card>
        <Card className="p-2">
          <VersionHistory versions={[]} />
        </Card>
      </section>

      <section className="space-y-3">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-400 dark:text-gray-500">
          EmptyState 家族（各页实际用到的四种）
        </h3>
        <div className="grid sm:grid-cols-2 gap-3">
          <Card>
            <EmptyState
              icon={DocumentTextIcon}
              title={zh.document.emptyTitle}
              description={zh.document.emptyDesc}
            />
          </Card>
          <Card>
            <EmptyState
              icon={BookOpenIcon}
              title={zh.reader.panel.emptyTitle}
              description={zh.reader.panel.emptyDesc}
            />
          </Card>
          <Card>
            <EmptyState
              icon={CircleStackIcon}
              title={zh.oaCases.emptyTitle}
              description={zh.oaCases.emptyDesc}
            />
          </Card>
          <Card>
            <EmptyState
              icon={PhotoIcon}
              title={zh.stages.figures.empty}
              description={zh.stages.figures.emptyDesc}
            />
          </Card>
        </div>
      </section>

      <section className="space-y-3">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-400 dark:text-gray-500">
          骨架屏（会话列表 / 案例行 / 文档正文）
        </h3>
        <div className="grid sm:grid-cols-3 gap-4">
          <div className="space-y-1.5">
            <Skeleton shimmer className="h-4 w-20 rounded" />
            <Skeleton shimmer className="h-7 w-full rounded-lg" />
            <Skeleton shimmer className="h-7 w-[85%] rounded-lg" />
          </div>
          <div className="space-y-3">
            <Skeleton shimmer className="h-24 rounded-2xl" />
          </div>
          <div className="space-y-2">
            <Skeleton shimmer className="h-6 w-2/5 rounded" />
            <Skeleton shimmer className="h-4 w-full rounded" />
            <Skeleton shimmer className="h-4 w-[86%] rounded" />
          </div>
        </div>
      </section>

      <section className="space-y-3">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-400 dark:text-gray-500">
          富文本溢出 QA（宽表格 / mermaid / KaTeX / 长行代码）
        </h3>
        <Card className="p-4">
          <StreamRow
            item={{ kind: 'assistant', id: 'rich', markdown: RICH_MARKDOWN, streaming: false }}
          />
        </Card>
      </section>
    </div>
  )
}
