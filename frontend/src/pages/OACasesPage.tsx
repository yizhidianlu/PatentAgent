import { useCallback, useMemo, useState } from 'react'
import {
  ArrowPathIcon,
  ArrowUpTrayIcon,
  ChevronDownIcon,
  CircleStackIcon,
  MagnifyingGlassIcon,
  TrashIcon,
  XMarkIcon,
} from '@heroicons/react/24/outline'
import { cn } from '../lib/cn'
import { zh } from '../i18n/zh'
import {
  isOaLibraryMissing,
  useOaLibraryDelete,
  useOaLibraryDetail,
  useOaLibraryIngest,
  useOaLibraryList,
  useOaLibraryRebuild,
  useOaLibrarySearch,
  useOaLibraryUpdate,
  type OaLibraryItem,
} from '../api/oa'
import { useUiStore } from '../stores/uiStore'
import { outcomeBadgeVariant, scorePercent } from '../lib/oaBadges'
import { Badge } from '../components/ui/Badge'
import { Button } from '../components/ui/Button'
import { Card } from '../components/ui/Card'
import { Drawer } from '../components/ui/Drawer'
import { Dropdown, DropdownItem } from '../components/ui/Dropdown'
import { EmptyState } from '../components/ui/EmptyState'
import { Input } from '../components/ui/Input'
import { Modal } from '../components/ui/Modal'
import { Select } from '../components/ui/Select'
import { Skeleton } from '../components/ui/Skeleton'
import { Spinner } from '../components/ui/Spinner'
import { Dropzone } from '../components/upload/Dropzone'
import { SelectedFiles } from '../components/upload/SelectedFiles'
import { MarkdownBlock } from '../components/markdown/MarkdownBlock'

const OUTCOME_KEYS = ['granted', 'amended_then_granted', 'rejected', 'pending', 'withdrawn', 'unknown']
const DEFECT_KEYS = [
  'novelty',
  'inventiveness',
  'clarity',
  'support',
  'disclosure',
  'formality',
  'other',
]

function formatDate(value: string): string {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value.slice(0, 10)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

/** 案例行卡片（标题 + 结果徽章 + 标签 chips + 日期；检索态另显相似度）。 */
function CaseRow({
  item,
  onOpen,
  onConfirm,
  confirming,
}: {
  item: OaLibraryItem
  onOpen: () => void
  onConfirm: () => void
  confirming: boolean
}) {
  return (
    <Card className="p-4 hover:border-indigo-300/70 dark:hover:border-indigo-500/40 transition-colors">
      <div className="flex items-start gap-3">
        <button type="button" onClick={onOpen} className="flex-1 min-w-0 text-left">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">
              {item.title}
            </span>
            {item.outcome && (
              <Badge variant={outcomeBadgeVariant(item.outcome)}>
                {zh.oa.labels.outcomes[item.outcome] ?? item.outcome}
              </Badge>
            )}
            {item.status === 'draft' && <Badge variant="amber">{zh.oaCases.statusDraft}</Badge>}
            {item.score !== undefined && (
              <Badge variant="indigo">
                {zh.oa.retrieve.score(scorePercent(item.score))}
              </Badge>
            )}
          </div>

          {item.chunkText && (
            <p className="mt-1 text-xs leading-relaxed text-gray-500 dark:text-gray-400 line-clamp-2">
              {item.chunkText}
            </p>
          )}

          <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
            {item.patentType && (
              <span className="px-1.5 py-0.5 rounded-full bg-gray-100 dark:bg-gray-700 text-[10px] text-gray-500 dark:text-gray-300">
                {zh.oa.labels.patentTypes[item.patentType] ?? item.patentType}
              </span>
            )}
            {item.defectTypes.slice(0, 3).map((defect) => (
              <span
                key={defect}
                className="px-1.5 py-0.5 rounded-full bg-gray-100 dark:bg-gray-700 text-[10px] text-gray-500 dark:text-gray-300"
              >
                {zh.oa.labels.defectTypes[defect] ?? defect}
              </span>
            ))}
            {item.tags.slice(0, 4).map((tag) => (
              <span
                key={tag}
                className="px-1.5 py-0.5 rounded-full bg-indigo-50 dark:bg-indigo-500/10 text-[10px] text-[#6366f1] dark:text-indigo-400"
              >
                {tag}
              </span>
            ))}
            <span className="ml-auto text-[10px] text-gray-400 dark:text-gray-500">
              {formatDate(item.updatedAt || item.createdAt)}
              {item.chunkCount > 0 ? ` · ${zh.oaCases.chunkCount(item.chunkCount)}` : ''}
            </span>
          </div>
        </button>

        {item.status === 'draft' && (
          <Button size="sm" onClick={onConfirm} disabled={confirming} className="shrink-0 gap-1.5">
            {confirming && <Spinner size="sm" className="border-white/40 border-t-white" />}
            {zh.oaCases.confirmAction}
          </Button>
        )}
      </div>
    </Card>
  )
}

/**
 * §3.6 OACasesPage（SecondaryLayout）：
 * 页头（标题 + 描述 + 导入案例 / 重建索引）；工具栏（语义检索 + 检索方式徽章 +
 * 结案结果 / 缺陷类型过滤）；案例行卡片列表；点击开右侧 Drawer 看笔记与 frontmatter；
 * 草稿态显示「待人审」徽章与「确认入库」（PUT status=confirmed）。
 */
export function OACasesPage() {
  const pushToast = useUiStore((s) => s.pushToast)

  const [draftQuery, setDraftQuery] = useState('')
  const [searchQuery, setSearchQuery] = useState('')
  const [outcome, setOutcome] = useState('')
  const [defectType, setDefectType] = useState('')
  const [openId, setOpenId] = useState<string | null>(null)
  const [importOpen, setImportOpen] = useState(false)
  const [rebuildOpen, setRebuildOpen] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<OaLibraryItem | null>(null)
  const [importFiles, setImportFiles] = useState<File[]>([])
  const [importNotes, setImportNotes] = useState('')
  const [importOutcome, setImportOutcome] = useState('')

  const listQuery = useOaLibraryList({ defectType: defectType || undefined })
  const search = useOaLibrarySearch(searchQuery, 12)
  const detail = useOaLibraryDetail(openId)
  const update = useOaLibraryUpdate()
  const remove = useOaLibraryDelete()
  const ingest = useOaLibraryIngest()
  const rebuild = useOaLibraryRebuild()

  const searching = searchQuery.trim().length > 0

  const items = useMemo<OaLibraryItem[]>(() => {
    const base = searching ? (search.data?.items ?? []) : (listQuery.data?.items ?? [])
    return base.filter((item) => {
      if (outcome && item.outcome !== outcome) return false
      if (searching && defectType && !item.defectTypes.includes(defectType)) return false
      return true
    })
  }, [searching, search.data, listQuery.data, outcome, defectType])

  const loading = searching ? search.isPending : listQuery.isPending
  const listError = searching ? search.error : listQuery.error
  const missing = isOaLibraryMissing(listError)

  const handleConfirm = useCallback(
    (item: OaLibraryItem) => {
      void update
        .mutateAsync({ id: item.id, status: 'confirmed' })
        .then(() => pushToast('info', zh.oaCases.confirmOk))
        .catch((e: unknown) => {
          const message = e instanceof Error ? e.message : ''
          pushToast('error', message || zh.oaCases.confirmFailed)
        })
    },
    [pushToast, update],
  )

  const handleDelete = useCallback(() => {
    if (!deleteTarget) return
    const id = deleteTarget.id
    setDeleteTarget(null)
    void remove
      .mutateAsync(id)
      .then(() => {
        if (openId === id) setOpenId(null)
        pushToast('info', zh.oaCases.deleteOk)
      })
      .catch((e: unknown) => {
        const message = e instanceof Error ? e.message : ''
        pushToast('error', message || zh.oaCases.deleteFailed)
      })
  }, [deleteTarget, openId, pushToast, remove])

  const handleImport = useCallback(() => {
    if (importFiles.length === 0) return
    void ingest
      .mutateAsync({ files: importFiles, notes: importNotes, outcome: importOutcome })
      .then((item) => {
        pushToast('info', zh.oaCases.importOk)
        setImportOpen(false)
        setImportFiles([])
        setImportNotes('')
        setImportOutcome('')
        if (item.id) setOpenId(item.id)
      })
      .catch((e: unknown) => {
        const message = e instanceof Error ? e.message : ''
        pushToast('error', message || zh.oaCases.importFailed)
      })
  }, [importFiles, importNotes, importOutcome, ingest, pushToast])

  const handleRebuild = useCallback(() => {
    setRebuildOpen(false)
    void rebuild
      .mutateAsync()
      .then((result) => pushToast('info', result.message || zh.oaCases.rebuildOk(result.queued)))
      .catch((e: unknown) => {
        const message = e instanceof Error ? e.message : ''
        pushToast('error', message || zh.oaCases.rebuildFailed)
      })
  }, [pushToast, rebuild])

  const detailItem = detail.data ?? items.find((i) => i.id === openId) ?? null

  return (
    <div className="flex-1 min-h-0 flex flex-col">
      {/* 页头 */}
      <header className="px-4 sm:px-8 py-4 bg-white dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700 shadow-sm">
        {/* 窄屏（390）下标题 + 两个操作钮并排会把描述挤成两字一行：
            sm 以下让按钮组整体换行到第二行，桌面维持右对齐单行。 */}
        <div className="max-w-4xl mx-auto flex flex-wrap items-start gap-x-3 gap-y-3">
          <div className="w-full sm:flex-1 sm:w-auto min-w-0">
            <h1 className="text-lg font-bold tracking-tight text-gray-900 dark:text-gray-100">
              {zh.oaCases.title}
            </h1>
            <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
              {zh.oaCases.subtitle}
            </p>
          </div>
          <div className="flex items-center gap-2 shrink-0 sm:ml-auto">
            <Button
              variant="secondary"
              size="sm"
              onClick={() => setRebuildOpen(true)}
              disabled={rebuild.isPending}
              className="shrink-0 gap-1.5"
            >
              {rebuild.isPending ? (
                <Spinner size="sm" />
              ) : (
                <ArrowPathIcon className="w-3.5 h-3.5" strokeWidth={1.5} />
              )}
              {zh.oaCases.rebuild}
            </Button>
            <Button size="sm" onClick={() => setImportOpen(true)} className="shrink-0 gap-1.5">
              <ArrowUpTrayIcon className="w-3.5 h-3.5" strokeWidth={1.5} />
              {zh.oaCases.import}
            </Button>
          </div>
        </div>
      </header>

      {/* 体 */}
      <div className="flex-1 min-h-0 overflow-y-auto">
        <div className="max-w-4xl mx-auto p-4 sm:p-8 space-y-4">
          {/* 工具栏 */}
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative flex-1 min-w-[200px]">
              <MagnifyingGlassIcon
                className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400"
                strokeWidth={1.5}
              />
              <Input
                value={draftQuery}
                placeholder={zh.oaCases.searchPlaceholder}
                onChange={(e) => setDraftQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault()
                    setSearchQuery(draftQuery.trim())
                  }
                }}
                className="pl-9"
                aria-label={zh.oaCases.searchPlaceholder}
              />
            </div>

            <Button size="sm" onClick={() => setSearchQuery(draftQuery.trim())}>
              {zh.oaCases.searchAction}
            </Button>

            {/* 结案结果过滤 */}
            <Dropdown
              align="right"
              panelClassName="w-48"
              trigger={({ toggle, open }) => (
                <button
                  type="button"
                  onClick={toggle}
                  className={cn(
                    'inline-flex items-center gap-1 h-10 px-3 rounded-xl border text-sm transition-colors',
                    'border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200',
                    open && 'border-[#6366f1] text-[#6366f1]',
                  )}
                >
                  {outcome ? (zh.oa.labels.outcomes[outcome] ?? outcome) : zh.oaCases.filterOutcome}
                  <ChevronDownIcon className="w-4 h-4" strokeWidth={1.5} />
                </button>
              )}
            >
              {({ close }) => (
                <>
                  <DropdownItem
                    onClick={() => {
                      setOutcome('')
                      close()
                    }}
                  >
                    {zh.oaCases.filterAll}
                  </DropdownItem>
                  {OUTCOME_KEYS.map((key) => (
                    <DropdownItem
                      key={key}
                      onClick={() => {
                        setOutcome(key)
                        close()
                      }}
                    >
                      {zh.oa.labels.outcomes[key] ?? key}
                    </DropdownItem>
                  ))}
                </>
              )}
            </Dropdown>

            {/* 缺陷类型过滤 */}
            <Dropdown
              align="right"
              panelClassName="w-56"
              trigger={({ toggle, open }) => (
                <button
                  type="button"
                  onClick={toggle}
                  className={cn(
                    'inline-flex items-center gap-1 h-10 px-3 rounded-xl border text-sm transition-colors',
                    'border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200',
                    open && 'border-[#6366f1] text-[#6366f1]',
                  )}
                >
                  {defectType
                    ? (zh.oa.labels.defectTypes[defectType] ?? defectType)
                    : zh.oaCases.filterDefect}
                  <ChevronDownIcon className="w-4 h-4" strokeWidth={1.5} />
                </button>
              )}
            >
              {({ close }) => (
                <>
                  <DropdownItem
                    onClick={() => {
                      setDefectType('')
                      close()
                    }}
                  >
                    {zh.oaCases.filterAll}
                  </DropdownItem>
                  {DEFECT_KEYS.map((key) => (
                    <DropdownItem
                      key={key}
                      onClick={() => {
                        setDefectType(key)
                        close()
                      }}
                    >
                      {zh.oa.labels.defectTypes[key] ?? key}
                    </DropdownItem>
                  ))}
                </>
              )}
            </Dropdown>
          </div>

          {/* 检索态：检索方式徽章 + 结果数 + 清除 */}
          {searching && (
            <div className="flex flex-wrap items-center gap-2">
              <span className="inline-flex items-center gap-1.5 px-2 py-1 rounded-full bg-indigo-50 dark:bg-indigo-500/10 text-[11px] font-medium text-[#6366f1] dark:text-indigo-400">
                <MagnifyingGlassIcon className="w-3.5 h-3.5" strokeWidth={1.5} />
                {zh.oa.retrieve.modeLabel}：
                {zh.oa.labels.retrievalModes[search.data?.retrievalMode ?? 'keyword'] ??
                  search.data?.retrievalMode}
              </span>
              <span className="text-[11px] text-gray-500 dark:text-gray-400">
                {search.isPending ? zh.oaCases.searching : zh.oaCases.resultCount(items.length)}
              </span>
              {(search.data?.message || search.data?.error || (search.error && !missing)) && (
                <span className="text-[11px] text-amber-600 dark:text-amber-400">
                  {search.data?.message || search.data?.error || zh.oaCases.searchFailed}
                </span>
              )}
              <button
                type="button"
                onClick={() => {
                  setSearchQuery('')
                  setDraftQuery('')
                }}
                className="inline-flex items-center gap-1 text-[11px] text-gray-500 dark:text-gray-400 hover:text-[#6366f1] transition-colors"
              >
                <XMarkIcon className="w-3.5 h-3.5" strokeWidth={1.5} />
                {zh.oaCases.searchClear}
              </button>
            </div>
          )}

          {/* 列表 / 骨架 / 空态 */}
          {loading ? (
            <div className="space-y-3">
              <Skeleton shimmer className="h-24 rounded-2xl" />
              <Skeleton shimmer className="h-24 rounded-2xl" />
              <Skeleton shimmer className="h-24 rounded-2xl" />
            </div>
          ) : items.length === 0 ? (
            <EmptyState
              icon={CircleStackIcon}
              title={
                listError && !missing
                  ? zh.oaCases.loadFailed
                  : searching
                    ? zh.oaCases.searchEmpty
                    : zh.oaCases.emptyTitle
              }
              description={searching ? zh.oa.retrieve.emptyDesc : zh.oaCases.emptyDesc}
              action={
                !searching ? (
                  <Button size="sm" onClick={() => setImportOpen(true)}>
                    {zh.oaCases.import}
                  </Button>
                ) : undefined
              }
            />
          ) : (
            <div className="space-y-3">
              {items.map((item) => (
                <CaseRow
                  key={item.id}
                  item={item}
                  onOpen={() => setOpenId(item.id)}
                  onConfirm={() => handleConfirm(item)}
                  confirming={update.isPending}
                />
              ))}
            </div>
          )}
        </div>
      </div>

      {/* 详情抽屉 */}
      <Drawer
        open={openId !== null}
        onClose={() => setOpenId(null)}
        title={detailItem?.title || zh.oaCases.detailTitle}
      >
        {detail.isPending ? (
          <div className="space-y-3">
            <Skeleton shimmer className="h-6 rounded-lg" />
            <Skeleton shimmer className="h-40 rounded-xl" />
          </div>
        ) : (
          <div className="space-y-4">
            <div className="flex flex-wrap items-center gap-1.5">
              {detailItem?.outcome && (
                <Badge variant={outcomeBadgeVariant(detailItem.outcome)}>
                  {zh.oa.labels.outcomes[detailItem.outcome] ?? detailItem.outcome}
                </Badge>
              )}
              <Badge variant={detailItem?.status === 'confirmed' ? 'emerald' : 'amber'}>
                {detailItem?.status === 'confirmed'
                  ? zh.oaCases.statusConfirmed
                  : zh.oaCases.statusDraft}
              </Badge>
              {detail.data && !detail.data.embedded && (
                <Badge variant="neutral">{zh.oaCases.notEmbedded}</Badge>
              )}
              {detailItem?.updatedAt && (
                <span className="ml-auto text-[11px] text-gray-400 dark:text-gray-500">
                  {zh.oaCases.updatedAt} {formatDate(detailItem.updatedAt)}
                </span>
              )}
            </div>

            {detailItem?.status === 'draft' && (
              <Button
                size="sm"
                onClick={() => detailItem && handleConfirm(detailItem)}
                disabled={update.isPending}
                className="gap-1.5"
              >
                {update.isPending && <Spinner size="sm" className="border-white/40 border-t-white" />}
                {zh.oaCases.confirmAction}
              </Button>
            )}

            {/* frontmatter */}
            <section className="space-y-1.5">
              <p className="text-[13px] font-medium text-gray-700 dark:text-gray-200">
                {zh.oaCases.detailFrontmatter}
              </p>
              <dl className="rounded-xl border border-gray-200 dark:border-gray-700 divide-y divide-gray-100 dark:divide-gray-700 text-[12px]">
                {Object.entries(detailItem?.frontmatter ?? {}).map(([key, value]) => (
                  <div key={key} className="flex gap-3 px-3 py-1.5">
                    <dt className="w-32 shrink-0 text-gray-400 dark:text-gray-500">{key}</dt>
                    <dd className="flex-1 min-w-0 break-words text-gray-700 dark:text-gray-200">
                      {Array.isArray(value)
                        ? value.map((v) => String(v)).join('、')
                        : typeof value === 'object' && value !== null
                          ? JSON.stringify(value)
                          : String(value ?? '')}
                    </dd>
                  </div>
                ))}
              </dl>
            </section>

            {/* 案例笔记 markdown */}
            <section className="space-y-1.5">
              <p className="text-[13px] font-medium text-gray-700 dark:text-gray-200">
                {zh.oaCases.detailNote}
              </p>
              {detail.data?.noteMd ? (
                <MarkdownBlock markdown={detail.data.noteMd} className="md-prose" />
              ) : (
                <p className="text-xs text-gray-400 dark:text-gray-500">{zh.oaCases.detailEmpty}</p>
              )}
            </section>

            {/* 脱敏登记 */}
            {(detail.data?.redactions.length ?? 0) > 0 && (
              <section className="space-y-1.5">
                <p className="text-[13px] font-medium text-gray-700 dark:text-gray-200">
                  {zh.oaCases.detailRedactions}
                </p>
                <ul className="space-y-1 text-[12px] text-gray-600 dark:text-gray-300 list-disc pl-5">
                  {detail.data?.redactions.map((r, i) => (
                    <li key={i}>
                      {r.originalHint} → {r.replacedWith}
                      {r.reason ? `（${r.reason}）` : ''}
                    </li>
                  ))}
                </ul>
              </section>
            )}

            {(detail.data?.inferredFields.length ?? 0) > 0 && (
              <p className="text-[11px] text-amber-600 dark:text-amber-400">
                {zh.oaCases.detailInferred}：{detail.data?.inferredFields.join('、')}
              </p>
            )}

            <div className="pt-2 border-t border-gray-100 dark:border-gray-700">
              <button
                type="button"
                onClick={() => detailItem && setDeleteTarget(detailItem)}
                className="inline-flex items-center gap-1.5 text-[12px] text-red-600 dark:text-red-400 hover:underline"
              >
                <TrashIcon className="w-3.5 h-3.5" strokeWidth={1.5} />
                {zh.oaCases.deleteAction}
              </button>
            </div>
          </div>
        )}
      </Drawer>

      {/* 导入案例 Modal */}
      <Modal open={importOpen} onClose={() => setImportOpen(false)} title={zh.oaCases.importTitle}>
        <div className="space-y-3">
          <p className="text-xs text-gray-500 dark:text-gray-400">{zh.oaCases.importDesc}</p>
          <Dropzone
            accept=".pdf,.doc,.docx,.md,.txt"
            title={zh.oaCases.importDrop}
            hint={zh.oaCases.importAccept}
            onFiles={(files) =>
              // 追加而非替换：分两次选文件时，替换会把前一批悄悄丢掉。
              // 同名同大小视为同一个文件，避免重复选中同一份。
              setImportFiles((prev) => {
                const seen = new Set(prev.map((f) => `${f.name}:${f.size}`))
                return [...prev, ...files.filter((f) => !seen.has(`${f.name}:${f.size}`))]
              })
            }
          />
          <SelectedFiles
            files={importFiles}
            onRemove={(index) =>
              setImportFiles((prev) => prev.filter((_, i) => i !== index))
            }
          />
          <label className="block space-y-1">
            <span className="block text-[12px] text-gray-500 dark:text-gray-400">
              {zh.oaCases.filterOutcome}
            </span>
            <Select value={importOutcome} onChange={(e) => setImportOutcome(e.target.value)}>
              <option value="">{zh.oaCases.filterAll}</option>
              {OUTCOME_KEYS.map((key) => (
                <option key={key} value={key}>
                  {zh.oa.labels.outcomes[key] ?? key}
                </option>
              ))}
            </Select>
          </label>
          <span className="block text-[12px] text-gray-500 dark:text-gray-400">
            {zh.oaCases.importNotesLabel}
          </span>
          <textarea
            value={importNotes}
            rows={3}
            placeholder={zh.oaCases.importNotesPlaceholder}
            onChange={(e) => setImportNotes(e.target.value)}
            className="w-full rounded-xl border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 px-3 py-2 text-[13px] text-gray-900 dark:text-gray-100 outline-none focus:border-[#6366f1] focus:ring-4 focus:ring-indigo-500/10 resize-y"
          />
          <div className="flex justify-end gap-2">
            <Button variant="ghost" size="sm" onClick={() => setImportOpen(false)}>
              {zh.oaCases.cancel}
            </Button>
            <Button
              size="sm"
              onClick={handleImport}
              disabled={importFiles.length === 0 || ingest.isPending}
              className="gap-1.5"
            >
              {ingest.isPending && <Spinner size="sm" className="border-white/40 border-t-white" />}
              {ingest.isPending ? zh.oaCases.importing : zh.oaCases.import}
            </Button>
          </div>
        </div>
      </Modal>

      {/* 删除确认 */}
      <Modal
        open={deleteTarget !== null}
        onClose={() => setDeleteTarget(null)}
        title={zh.oaCases.deleteConfirmTitle}
      >
        <div className="space-y-4">
          <p className="text-sm text-gray-600 dark:text-gray-300">
            {zh.oaCases.deleteConfirmDesc}
          </p>
          <div className="flex justify-end gap-2">
            <Button variant="ghost" size="sm" onClick={() => setDeleteTarget(null)}>
              {zh.oaCases.cancel}
            </Button>
            <Button variant="danger" size="sm" onClick={handleDelete}>
              {zh.oaCases.deleteAction}
            </Button>
          </div>
        </div>
      </Modal>

      {/* 重建索引确认 */}
      <Modal
        open={rebuildOpen}
        onClose={() => setRebuildOpen(false)}
        title={zh.oaCases.rebuildConfirmTitle}
      >
        <div className="space-y-4">
          <p className="text-sm text-gray-600 dark:text-gray-300">
            {zh.oaCases.rebuildConfirmDesc}
          </p>
          <div className="flex justify-end gap-2">
            <Button variant="ghost" size="sm" onClick={() => setRebuildOpen(false)}>
              {zh.oaCases.cancel}
            </Button>
            <Button size="sm" onClick={handleRebuild}>
              {zh.oaCases.rebuild}
            </Button>
          </div>
        </div>
      </Modal>
    </div>
  )
}
