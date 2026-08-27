import { useMemo, useState } from 'react'
import {
  ArrowDownTrayIcon,
  CheckIcon,
  PhotoIcon,
} from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { artifactDownloadUrl, downloadUrl, fileDownloadUrl } from '../../lib/download'
import { EmptyState } from '../ui/EmptyState'
import { Modal } from '../ui/Modal'
import { StageCardShell, type StageCardProps } from '../pipeline/StageCardShell'
import type { InteractionRequiredEvent } from '../../types/stream'

/** 附图条目（宽松契约：url / artifact_id / file_id 三选一即可预览下载）。 */
interface FigureItem {
  id: string
  label: string
  caption: string
  url: string | null
  filename: string
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

function str(value: unknown): string {
  return typeof value === 'string' ? value : typeof value === 'number' ? String(value) : ''
}

/** 从 interaction_required 载荷各处捞出附图数组（default.figures / schema.figures / default）。 */
function readFigures(payload: InteractionRequiredEvent | null): FigureItem[] {
  const candidates: unknown[] = []
  const rawDefault: unknown = payload?.default
  const def = asRecord(rawDefault)
  const schema = asRecord(payload?.schema)
  if (def && Array.isArray(def.figures)) candidates.push(...(def.figures as unknown[]))
  else if (Array.isArray(rawDefault)) candidates.push(...(rawDefault as unknown[]))
  else if (schema && Array.isArray(schema.figures)) candidates.push(...(schema.figures as unknown[]))

  return candidates.map((raw, i) => {
    const item = asRecord(raw)
    const id = str(item?.id) || str(item?.figure_id) || String(i + 1)
    const number = str(item?.number) || str(item?.label) || String(i + 1)
    const artifactId = str(item?.artifact_id)
    const fileId = str(item?.file_id)
    const direct = str(item?.url) || str(item?.src)
    return {
      id,
      label: str(item?.label) || zh.stages.figures.figureLabel(number),
      caption: str(item?.caption) || str(item?.title) || str(item?.description),
      url: direct || (artifactId ? artifactDownloadUrl(artifactId) : fileId ? fileDownloadUrl(fileId) : null),
      filename: str(item?.filename) || `${id}.svg`,
    }
  })
}

/**
 * §3.3 FiguresPreviewCard：附图网格（tile + `aspect-[3/4]` 预览区 + 图注）、
 * 点击开 lightbox Modal（max-w-3xl + 下载），勾选后提交 {figure_ids}。
 */
export function FiguresPreviewCard({ stage, submit, skip, busy }: StageCardProps) {
  const payload = stage.payload as InteractionRequiredEvent | null
  const figures = useMemo(() => readFigures(payload), [payload])

  const resultIds = (stage.result as { figure_ids?: string[] } | undefined)?.figure_ids
  const [selected, setSelected] = useState<string[]>(
    () => resultIds ?? figures.map((f) => f.id),
  )
  const [lightbox, setLightbox] = useState<FigureItem | null>(null)

  const toggle = (id: string) =>
    setSelected((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]))

  const download = (figure: FigureItem) => {
    if (!figure.url) return
    downloadUrl(figure.url, figure.filename)
  }

  return (
    <StageCardShell
      stage={stage}
      icon={PhotoIcon}
      summary={zh.stages.figures.summary(resultIds?.length ?? selected.length)}
      onConfirm={() => submit({ figure_ids: selected })}
      onSkip={skip}
      confirmLabel={zh.stages.figures.confirm}
      busy={busy}
    >
      <div className="space-y-3">
        {payload?.prompt && (
          <p className="text-sm text-gray-700 dark:text-gray-300">{payload.prompt}</p>
        )}

        {figures.length === 0 ? (
          <EmptyState
            icon={PhotoIcon}
            title={zh.stages.figures.empty}
            description={zh.stages.figures.emptyDesc}
            className="py-6"
          />
        ) : (
          <>
            <p className="text-xs text-gray-500 dark:text-gray-400">
              {zh.stages.figures.selectedCount(selected.length, figures.length)}
            </p>
            <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
              {figures.map((figure) => {
                const active = selected.includes(figure.id)
                return (
                  <div
                    key={figure.id}
                    className={cn(
                      'rounded-xl border p-2 transition-colors',
                      active
                        ? 'border-orange-400 dark:border-orange-500/60 bg-orange-50/40 dark:bg-orange-500/5'
                        : 'border-gray-200 dark:border-gray-700',
                    )}
                  >
                    <button
                      type="button"
                      onClick={() => setLightbox(figure)}
                      title={zh.stages.figures.preview}
                      className="block w-full aspect-[3/4] bg-gray-50 dark:bg-gray-900 rounded-lg overflow-hidden"
                    >
                      {figure.url ? (
                        <img
                          src={figure.url}
                          alt={figure.caption || figure.label}
                          loading="lazy"
                          className="w-full h-full object-contain"
                        />
                      ) : (
                        <span className="w-full h-full flex items-center justify-center text-[11px] text-gray-400">
                          {zh.stages.figures.noPreview}
                        </span>
                      )}
                    </button>
                    <div className="flex items-center gap-1 mt-1.5">
                      <span className="text-xs text-gray-600 dark:text-gray-300 truncate flex-1 min-w-0">
                        {figure.label}
                        {figure.caption ? ` · ${figure.caption}` : ''}
                      </span>
                      <button
                        type="button"
                        onClick={() => toggle(figure.id)}
                        aria-label={active ? zh.stages.figures.unselect : zh.stages.figures.select}
                        title={active ? zh.stages.figures.unselect : zh.stages.figures.select}
                        className={cn(
                          'w-5 h-5 shrink-0 rounded-full border flex items-center justify-center transition-colors',
                          active
                            ? 'bg-orange-500 border-orange-500 text-white'
                            : 'border-gray-300 dark:border-gray-600 text-transparent hover:border-orange-400',
                        )}
                      >
                        <CheckIcon className="w-3 h-3" strokeWidth={2.5} />
                      </button>
                    </div>
                  </div>
                )
              })}
            </div>
          </>
        )}
      </div>

      {/* lightbox */}
      <Modal
        open={lightbox !== null}
        onClose={() => setLightbox(null)}
        title={lightbox ? `${lightbox.label}${lightbox.caption ? ` · ${lightbox.caption}` : ''}` : ''}
        panelClassName="max-w-3xl"
      >
        <div className="space-y-3">
          <div className="w-full max-h-[70vh] bg-gray-50 dark:bg-gray-900 rounded-xl overflow-hidden flex items-center justify-center">
            {lightbox?.url ? (
              <img
                src={lightbox.url}
                alt={lightbox.caption || lightbox.label}
                className="max-h-[70vh] w-auto object-contain"
              />
            ) : (
              <span className="py-16 text-sm text-gray-400">{zh.stages.figures.noPreview}</span>
            )}
          </div>
          <div className="flex items-center justify-between">
            <span className="text-xs text-gray-400">{zh.stages.figures.lightboxHint}</span>
            <button
              type="button"
              disabled={!lightbox?.url}
              onClick={() => lightbox && download(lightbox)}
              className={cn(
                'inline-flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-medium',
                'border border-gray-200 dark:border-gray-600 text-gray-600 dark:text-gray-300',
                'hover:border-indigo-400/60 hover:text-[#6366f1] transition-colors',
                'disabled:opacity-50 disabled:pointer-events-none',
              )}
            >
              <ArrowDownTrayIcon className="w-3.5 h-3.5" strokeWidth={1.5} />
              {zh.stages.figures.download}
            </button>
          </div>
        </div>
      </Modal>
    </StageCardShell>
  )
}
