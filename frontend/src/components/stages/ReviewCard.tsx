import { useMemo, useState } from 'react'
import { ClipboardDocumentCheckIcon, CodeBracketIcon } from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { useUiStore } from '../../stores/uiStore'
import { useP2pContent, useUpdateP2pContent, type PatentContent } from '../../api/pipeline'
import { Button } from '../ui/Button'
import { Drawer } from '../ui/Drawer'
import { Spinner } from '../ui/Spinner'
import { StageCardShell, type StageCardProps } from '../pipeline/StageCardShell'
import type { InteractionRequiredEvent } from '../../types/stream'

type TabKey = 'abstract' | 'abstractFigure' | 'claims' | 'description' | 'drawings'

const TABS: readonly TabKey[] = [
  'abstract',
  'abstractFigure',
  'claims',
  'description',
  'drawings',
]

// ---------------------------------------------------------------------------
// 宽松取值工具（后端 PatentContent 字段可能演进，一律容错渲染）
// ---------------------------------------------------------------------------

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

/** 把任意值压成可读文本（对象取常见文本字段，否则 JSON）。 */
function toText(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  if (Array.isArray(value)) return value.map(toText).filter(Boolean).join('\n')
  const record = asRecord(value)
  if (record) {
    for (const key of ['text', 'content', 'description', 'caption', 'title', 'value']) {
      const v = record[key]
      if (typeof v === 'string' && v.trim()) return v
    }
    return JSON.stringify(value, null, 2)
  }
  return String(value)
}

/** 条目序号（claims/drawings 常见 number / no / index 字段）。 */
function itemNumber(value: unknown, fallback: number): number | string {
  const record = asRecord(value)
  if (record) {
    for (const key of ['number', 'no', 'index', 'label', 'figure']) {
      const v = record[key]
      if (typeof v === 'number' || (typeof v === 'string' && v.trim())) return v
    }
  }
  return fallback
}

const emptyLine = (
  <p className="text-sm text-gray-400 dark:text-gray-500">{zh.stages.review.empty}</p>
)

function TextBlock({ text }: { text: string }) {
  if (!text.trim()) return emptyLine
  return (
    <p className="text-sm leading-relaxed text-gray-700 dark:text-gray-300 whitespace-pre-wrap break-words">
      {text}
    </p>
  )
}

function ListBlock({
  items,
  label,
}: {
  items: unknown[]
  label: (n: number | string) => string
}) {
  if (items.length === 0) return emptyLine
  return (
    <ol className="space-y-2.5">
      {items.map((item, i) => (
        <li key={i} className="text-sm leading-relaxed text-gray-700 dark:text-gray-300">
          <span className="mr-1.5 font-medium text-gray-900 dark:text-gray-100">
            {label(itemNumber(item, i + 1))}
          </span>
          <span className="whitespace-pre-wrap break-words">{toText(item)}</span>
        </li>
      ))}
    </ol>
  )
}

function DescriptionBlock({ description }: { description: Record<string, unknown> | null }) {
  const entries = description ? Object.entries(description) : []
  if (entries.length === 0) return emptyLine
  return (
    <div className="space-y-4">
      {entries.map(([key, value]) => (
        <section key={key}>
          <h4 className="text-xs font-semibold text-gray-500 dark:text-gray-400 mb-1">
            {zh.stages.review.descriptionSections[key] ?? key}
          </h4>
          <p className="text-sm leading-relaxed text-gray-700 dark:text-gray-300 whitespace-pre-wrap break-words">
            {toText(value) || zh.stages.review.empty}
          </p>
        </section>
      ))}
    </div>
  )
}

/**
 * §3.3 p2p content_review 卡：五大部分只读预览 tabs + 「编辑 JSON」抽屉
 * （textarea 校验后 PUT /cases/{id}/p2p/content）+ 确认继续（POST /pipeline/input）。
 */
export function ReviewCard({ caseId, stage, submit, skip, busy }: StageCardProps) {
  const payload = stage.payload as InteractionRequiredEvent | null
  const pushToast = useUiStore((s) => s.pushToast)
  const [tab, setTab] = useState<TabKey>('abstract')
  const [editorOpen, setEditorOpen] = useState(false)
  const [draft, setDraft] = useState('')
  const [jsonError, setJsonError] = useState<string | null>(null)

  // 内容来源：GET /p2p/content 优先；后端未就绪时回退 interaction_required.default
  const contentQuery = useP2pContent(caseId, stage.status === 'active')
  const updateContent = useUpdateP2pContent(caseId)

  const content: PatentContent | null = useMemo(() => {
    if (contentQuery.data) return contentQuery.data
    const fromDefault = asRecord(payload?.default)
    if (fromDefault && ('claims' in fromDefault || 'abstract' in fromDefault)) {
      return fromDefault as PatentContent
    }
    const nested = asRecord(fromDefault?.content)
    return (nested as PatentContent | null) ?? null
  }, [contentQuery.data, payload?.default])

  const openEditor = () => {
    setDraft(JSON.stringify(content ?? {}, null, 2))
    setJsonError(null)
    setEditorOpen(true)
  }

  const saveJson = async () => {
    let parsed: unknown
    try {
      parsed = JSON.parse(draft)
    } catch {
      setJsonError(zh.stages.review.jsonInvalid)
      return
    }
    if (!asRecord(parsed)) {
      setJsonError(zh.stages.review.jsonInvalid)
      return
    }
    setJsonError(null)
    try {
      await updateContent.mutateAsync(parsed as PatentContent)
      pushToast('success', zh.stages.review.saved)
      setEditorOpen(false)
    } catch (e) {
      const message = e instanceof Error ? e.message : ''
      pushToast('error', message || zh.stages.review.saveFailed)
    }
  }

  const claimsRaw = content?.claims
  const claims: unknown[] = Array.isArray(claimsRaw) ? claimsRaw : []
  const drawingsRaw = content?.drawings
  const drawings: unknown[] = Array.isArray(drawingsRaw) ? drawingsRaw : []

  const tabBody = () => {
    if (contentQuery.isPending && !content) {
      return (
        <div className="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
          <Spinner size="sm" />
          {zh.stages.review.loading}
        </div>
      )
    }
    switch (tab) {
      case 'abstract':
        return <TextBlock text={toText(content?.abstract)} />
      case 'abstractFigure':
        return <TextBlock text={toText(content?.abstract_drawing)} />
      case 'claims':
        return <ListBlock items={claims} label={zh.stages.review.claimItem} />
      case 'description':
        return <DescriptionBlock description={asRecord(content?.description)} />
      case 'drawings':
        return <ListBlock items={drawings} label={zh.stages.review.drawingItem} />
    }
  }

  return (
    <StageCardShell
      stage={stage}
      icon={ClipboardDocumentCheckIcon}
      title={zh.stages.review.title}
      summary={zh.stages.review.summary}
      onConfirm={() => submit({ confirmed: true })}
      onSkip={skip}
      confirmLabel={zh.stages.review.confirmContinue}
      busy={busy}
    >
      <div className="space-y-3">
        {payload?.prompt && (
          <p className="text-sm text-gray-700 dark:text-gray-300">{payload.prompt}</p>
        )}

        {content?.invention_name && (
          <p className="text-xs text-gray-500 dark:text-gray-400">
            {zh.stages.review.inventionName}：
            <span className="text-gray-800 dark:text-gray-200">
              {String(content.invention_name)}
            </span>
          </p>
        )}

        {/* tabs */}
        <div className="flex items-center gap-1 overflow-x-auto scrollbar-thin border-b border-gray-100 dark:border-gray-700">
          {TABS.map((key) => (
            <button
              key={key}
              type="button"
              onClick={() => setTab(key)}
              className={cn(
                'shrink-0 px-3 py-2 text-xs font-medium border-b-2 -mb-px transition-colors',
                tab === key
                  ? 'border-[#6366f1] text-[#6366f1] dark:text-indigo-400'
                  : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200',
              )}
            >
              {zh.stages.review.tabs[key]}
            </button>
          ))}
          <div className="flex-1" />
          <button
            type="button"
            onClick={openEditor}
            className={cn(
              'shrink-0 inline-flex items-center gap-1 px-2 py-1 mb-1 rounded-lg',
              'text-xs font-medium text-gray-500 dark:text-gray-400',
              'hover:bg-gray-100 dark:hover:bg-gray-700 hover:text-gray-700 dark:hover:text-gray-200 transition-colors',
            )}
          >
            <CodeBracketIcon className="w-3.5 h-3.5" strokeWidth={1.5} />
            {zh.stages.review.editJson}
          </button>
        </div>

        <div className="max-h-72 overflow-y-auto scrollbar-thin pr-1">
          {contentQuery.isError && !content ? (
            <p className="text-xs text-amber-600 dark:text-amber-400">
              {zh.stages.review.loadFailed}
            </p>
          ) : (
            tabBody()
          )}
        </div>
      </div>

      {/* 编辑 JSON 抽屉 */}
      <Drawer
        open={editorOpen}
        onClose={() => setEditorOpen(false)}
        title={zh.stages.review.editorTitle}
      >
        <div className="flex flex-col h-full gap-3">
          <p className="text-xs text-gray-500 dark:text-gray-400">
            {zh.stages.review.editorHint}
          </p>
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            spellCheck={false}
            className={cn(
              'flex-1 min-h-[320px] w-full rounded-xl border px-3.5 py-2.5 font-mono text-[12px] leading-relaxed',
              'bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 scrollbar-thin outline-none',
              'transition-colors focus:ring-4 focus:ring-indigo-500/10',
              jsonError
                ? 'border-red-400 dark:border-red-500/60'
                : 'border-gray-300 dark:border-gray-600 focus:border-[#6366f1]',
            )}
          />
          {jsonError && <p className="text-xs text-red-500">{jsonError}</p>}
          <div className="flex justify-end gap-2">
            <Button variant="ghost" size="sm" onClick={() => setEditorOpen(false)}>
              {zh.common.cancel}
            </Button>
            <Button
              size="sm"
              className="gap-1.5"
              disabled={updateContent.isPending}
              onClick={() => void saveJson()}
            >
              {updateContent.isPending && (
                <Spinner size="sm" className="border-white/40 border-t-white" />
              )}
              {zh.stages.review.validateAndSave}
            </Button>
          </div>
        </div>
      </Drawer>
    </StageCardShell>
  )
}
