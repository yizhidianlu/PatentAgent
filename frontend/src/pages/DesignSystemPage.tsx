import { useEffect, useRef, useState, type ReactNode } from 'react'
import { EllipsisHorizontalIcon } from '@heroicons/react/24/outline'
import { zh } from '../i18n/zh'
import { useUiStore } from '../stores/uiStore'
import { Button } from '../components/ui/Button'
import { Card } from '../components/ui/Card'
import { Badge } from '../components/ui/Badge'
import { Skeleton } from '../components/ui/Skeleton'
import { Spinner } from '../components/ui/Spinner'
import { EmptyState } from '../components/ui/EmptyState'
import { Input } from '../components/ui/Input'
import { Select } from '../components/ui/Select'
import { ToggleSwitch } from '../components/ui/ToggleSwitch'
import { SegmentedToggle } from '../components/ui/SegmentedToggle'
import { Modal } from '../components/ui/Modal'
import { Drawer } from '../components/ui/Drawer'
import { Dropdown, DropdownItem } from '../components/ui/Dropdown'
import { ThemeToggle } from '../components/theme/ThemeToggle'
import { Composer } from '../components/composer/Composer'
import { SendButton } from '../components/composer/SendButton'
import { StepProgress } from '../components/pipeline/StepProgress'
import { StreamItemView } from '../components/pipeline/StreamItemView'
import { emptySession } from '../stores/sessionStore'
import { useComposerStore } from '../stores/composerStore'
import { DsPatterns } from './designSystem/DsPatterns'
import { DsStageGallery } from './designSystem/DsStageGallery'

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="space-y-3">
      <h2 className="text-sm font-semibold text-gray-500 dark:text-gray-400">{title}</h2>
      <div className="flex flex-wrap items-center gap-3">{children}</div>
    </section>
  )
}

/** 纵向堆叠的分节（内容宽而非并排排布时用）。 */
function Stack({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="space-y-3">
      <h2 className="text-sm font-semibold text-gray-500 dark:text-gray-400">{title}</h2>
      {children}
    </section>
  )
}

/**
 * 亮暗并排框。dark 变体为 `&:where(.dark, .dark *)`：给右栏套一层 `.dark`
 * 即可在不切全局主题的前提下真实渲染暗色态。
 * 左栏跟随当前全局主题——像素 QA 时把全局切到浅色，即得标准的亮暗并排；
 * 全局已是深色时左右两栏都是暗色（此时用右上角 ThemeToggle 切回浅色即可）。
 */
function LightDark({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="space-y-3">
      <h2 className="text-sm font-semibold text-gray-500 dark:text-gray-400">{title}</h2>
      <div className="grid lg:grid-cols-2 gap-4">
        <div className="rounded-2xl border border-gray-200 dark:border-gray-700 overflow-hidden">
          <p className="px-3 py-1.5 text-[11px] font-medium text-gray-400 border-b border-gray-100 dark:border-gray-700">
            当前主题
          </p>
          <div className="bg-white dark:bg-gray-900 p-4">{children}</div>
        </div>
        <div className="rounded-2xl border border-gray-200 dark:border-gray-700 overflow-hidden">
          <p className="px-3 py-1.5 text-[11px] font-medium text-gray-400 border-b border-gray-100 dark:border-gray-700">
            强制深色（.dark 子树）
          </p>
          <div className="dark bg-gray-900 p-4">{children}</div>
        </div>
      </div>
    </section>
  )
}

/** §3.8 DesignSystemPage（dev-only）：M1 原语像素 QA 台。 */
export function DesignSystemPage() {
  const pushToast = useUiStore((s) => s.pushToast)
  const [modalOpen, setModalOpen] = useState(false)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [toggleOn, setToggleOn] = useState(true)
  const [segment, setSegment] = useState<'a' | 'b' | 'c' | 'd'>('a')

  // M2：Composer 附件状态展示（uploading / done / error）——种子数据只造一次
  const composerSeeded = useRef(false)
  useEffect(() => {
    if (composerSeeded.current) return
    composerSeeded.current = true
    const store = useComposerStore.getState()
    if ((store.contexts['ds-attachments']?.attachments.length ?? 0) > 0) return
    const mk = (name: string, type: string, size: number) =>
      new File([new Uint8Array(size)], name, { type })
    const created = store.addFiles(
      'ds-attachments',
      [
        mk('项目技术方案.pdf', 'application/pdf', 240 * 1024),
        mk(
          '实验数据说明.docx',
          'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
          96 * 1024,
        ),
        mk('原型照片.png', 'image/png', 320 * 1024),
      ],
      'done',
    )
    const [uploading, , failed] = created
    if (uploading)
      store.updateAttachment('ds-attachments', uploading.localId, {
        status: 'uploading',
        progress: 45,
      })
    if (failed)
      store.updateAttachment('ds-attachments', failed.localId, {
        status: 'error',
        error: zh.composer.uploadError,
      })
  }, [])

  return (
    <div className="flex-1 p-6 sm:p-10 space-y-8 max-w-4xl mx-auto w-full">
      <header className="space-y-1">
        <h1 className="text-xl font-bold text-gray-900 dark:text-white">{zh.pages.designSystem}</h1>
        <p className="text-xs text-gray-500 dark:text-gray-400">
          全部 UI 原语 × 全状态 + StageCard 画廊 + 页面级模式（步骤条 / 流条目 / 版本历史 /
          空态 / 骨架 / 富文本溢出）。像素 QA 时以本页为准；把窗口拖到 390px
          可一次性验证全部窄屏降级。
        </p>
      </header>

      {/* ===== 亮暗并排：最常回归的几个原语 ===== */}
      <LightDark title="亮暗并排 · Button / Badge / Input / Card / EmptyState">
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <Button variant="primary" size="sm">
              {zh.common.confirm}
            </Button>
            <Button variant="secondary" size="sm">
              {zh.common.cancel}
            </Button>
            <Button variant="ghost" size="sm">
              {zh.common.skip}
            </Button>
            <Button variant="danger" size="sm">
              {zh.common.delete}
            </Button>
            <Button variant="primary" size="sm" disabled>
              disabled
            </Button>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Badge>默认</Badge>
            <Badge variant="gradient">Beta</Badge>
            <Badge variant="emerald">授权</Badge>
            <Badge variant="amber">审中</Badge>
            <Badge variant="red">驳回</Badge>
            <Badge variant="indigo">V3</Badge>
          </div>
          <Input placeholder="CN202410XXXXXX.X" />
          <Card className="p-3 text-sm text-gray-700 dark:text-gray-200">普通卡片</Card>
          <Card>
            <EmptyState title={zh.sidebar.empty.title} description={zh.sidebar.empty.description} />
          </Card>
        </div>
      </LightDark>

      <Section title="Button">
        <Button variant="primary">{zh.common.confirm}</Button>
        <Button variant="gradient">{zh.common.confirm}</Button>
        <Button variant="secondary">{zh.common.cancel}</Button>
        <Button variant="ghost">{zh.common.skip}</Button>
        <Button variant="danger">{zh.common.delete}</Button>
        <Button variant="primary" disabled>
          {zh.common.confirm}
        </Button>
      </Section>

      <Section title="Badge">
        <Badge>默认</Badge>
        <Badge variant="gradient">Beta</Badge>
        <Badge variant="emerald">授权</Badge>
        <Badge variant="amber">审中</Badge>
        <Badge variant="red">驳回</Badge>
        <Badge variant="indigo">V3</Badge>
      </Section>

      <Section title="Input / Select / ToggleSwitch / SegmentedToggle">
        <Input placeholder="CN202410XXXXXX.X" className="w-56" />
        <Select className="w-40" defaultValue="1" aria-label="Select 演示">
          <option value="1">选项一</option>
          <option value="2">选项二</option>
        </Select>
        <ToggleSwitch checked={toggleOn} onChange={setToggleOn} label="开关" />
        <SegmentedToggle
          options={[
            { value: 'a', label: zh.sidebar.groups.disclosure },
            { value: 'b', label: zh.sidebar.groups.paper },
            { value: 'c', label: zh.sidebar.groups.reader },
            { value: 'd', label: zh.sidebar.groups.oa },
          ]}
          value={segment}
          onChange={setSegment}
        />
      </Section>

      <Section title="Skeleton / Spinner">
        <Skeleton className="w-40 h-6" />
        <Skeleton shimmer className="w-40 h-6" />
        <Spinner size="sm" />
        <Spinner />
        <Spinner size="lg" />
      </Section>

      <Section title="Overlay（Toast / Modal / Drawer / Dropdown / ThemeToggle）">
        <Button variant="secondary" onClick={() => pushToast('success', '操作成功')}>
          Toast 成功
        </Button>
        <Button variant="secondary" onClick={() => pushToast('warning', '请注意检查输入')}>
          Toast 警告
        </Button>
        <Button variant="secondary" onClick={() => pushToast('error', '请求失败，请重试')}>
          Toast 错误
        </Button>
        <Button variant="secondary" onClick={() => setModalOpen(true)}>
          Modal
        </Button>
        <Button variant="secondary" onClick={() => setDrawerOpen(true)}>
          Drawer
        </Button>
        <Dropdown
          align="left"
          panelClassName="w-40"
          trigger={({ toggle }) => (
            <Button variant="secondary" onClick={toggle}>
              <EllipsisHorizontalIcon className="w-4 h-4" strokeWidth={1.5} />
              Dropdown
            </Button>
          )}
        >
          {({ close }) => (
            <>
              <DropdownItem onClick={close}>{zh.sidebar.itemMenu.rename}</DropdownItem>
              <DropdownItem danger onClick={close}>
                {zh.sidebar.itemMenu.delete}
              </DropdownItem>
            </>
          )}
        </Dropdown>
        <ThemeToggle />
      </Section>

      <Section title="Card / EmptyState">
        <Card className="p-4 w-64 text-sm text-gray-700 dark:text-gray-200">普通卡片</Card>
        <Card accent className="p-4 w-64 text-sm text-gray-700 dark:text-gray-200">
          Accent 卡片
        </Card>
        <Card className="w-72">
          <EmptyState title={zh.common.empty} description={zh.common.underConstructionDesc} />
        </Card>
      </Section>

      {/* ===== M2：Composer 各状态（frontend-design.md §2.3）===== */}
      <Section title="Composer · hero / indigo（空态，聚焦看光晕）">
        <Composer
          contextId="ds-hero-indigo"
          variant="hero"
          accent="indigo"
          placeholder={zh.home.placeholders.disclosure}
          onSend={() => pushToast('info', '发送（演示）')}
          fileAccept=".pdf,.doc,.docx,.ppt,.pptx,.md,.txt"
          imageAccept="image/*"
        />
      </Section>

      <Section title="Composer · hero / orange（论文转专利变体）">
        <Composer
          contextId="ds-hero-orange"
          variant="hero"
          accent="orange"
          placeholder={zh.home.placeholders.paper}
          onSend={() => pushToast('info', '发送（演示）')}
          fileAccept=".pdf"
          imageAccept="image/*"
        />
      </Section>

      <Section title="Composer · 附件三态（uploading 45% / done / error 重试）">
        <Composer
          contextId="ds-attachments"
          variant="hero"
          accent="indigo"
          placeholder={zh.home.placeholders.disclosure}
          onSend={() => pushToast('info', '发送（演示）')}
        />
      </Section>

      <Section title="Composer · busy（发送钮变 StopIcon）">
        <Composer
          contextId="ds-busy"
          variant="hero"
          accent="indigo"
          placeholder={zh.home.placeholders.disclosure}
          onSend={() => pushToast('info', '发送（演示）')}
          busy
          onStop={() => pushToast('warning', '已停止（演示）')}
        />
      </Section>

      <Section title="Composer · chat 变体（sticky + 免责声明）">
        <div className="w-full">
          <Composer
            contextId="ds-chat"
            variant="chat"
            accent="indigo"
            placeholder={zh.home.placeholders.disclosure}
            onSend={() => pushToast('info', '发送（演示）')}
          />
        </div>
      </Section>

      <Section title="SendButton 三态 × accent">
        <SendButton state="disabled" onClick={() => undefined} />
        <SendButton state="ready" accent="indigo" onClick={() => pushToast('info', '发送（演示）')} />
        <SendButton state="ready" accent="orange" onClick={() => pushToast('info', '发送（演示）')} />
        <SendButton state="busy" accent="indigo" onClick={() => pushToast('warning', '已停止（演示）')} />
        <SendButton state="busy" accent="orange" onClick={() => pushToast('warning', '已停止（演示）')} />
      </Section>

      {/* ===== M7：页面级模式 + StageCard 全量画廊 ===== */}
      <Stack title="页面级模式（步骤条 / 流条目 / 版本历史 / 空态 / 骨架 / 富文本溢出）">
        <DsPatterns />
      </Stack>

      <LightDark title="亮暗并排 · 流水线失败卡与步骤条">
        <div className="space-y-3">
          <StepProgress
            steps={[
              { key: 'a', label: '材料扫描', status: 'done' },
              { key: 'b', label: '联网查新', status: 'failed' },
              { key: 'c', label: '生成全文', status: 'pending' },
            ]}
          />
          <StreamItemView
            item={{
              kind: 'notice',
              id: 'ds-err',
              text: '国知局检索超时（Playwright 30s 未返回），本轮未取到命中结果。',
              level: 'error',
              retryable: true,
              stepKey: 'prior_art_search',
              stepLabel: '联网查新',
            }}
            caseId="ds"
            session={emptySession()}
            onStageSubmit={() => undefined}
            onStageSkip={() => undefined}
            onRetryStep={() => undefined}
          />
        </div>
      </LightDark>

      <Stack title="StageCard 画廊（注册表全量 × 全状态）">
        <DsStageGallery />
      </Stack>

      <Modal open={modalOpen} onClose={() => setModalOpen(false)} title="示例弹窗">
        <p className="text-sm text-gray-600 dark:text-gray-300">{zh.common.aiDisclaimer}</p>
        <div className="mt-5 flex justify-end gap-2">
          <Button variant="ghost" onClick={() => setModalOpen(false)}>
            {zh.common.cancel}
          </Button>
          <Button onClick={() => setModalOpen(false)}>{zh.common.confirm}</Button>
        </div>
      </Modal>

      <Drawer open={drawerOpen} onClose={() => setDrawerOpen(false)} title="示例抽屉">
        <p className="text-sm text-gray-600 dark:text-gray-300">{zh.common.underConstructionDesc}</p>
      </Drawer>
    </div>
  )
}
