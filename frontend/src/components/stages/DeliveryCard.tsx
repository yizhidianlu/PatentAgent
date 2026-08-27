import { useMemo } from 'react'
import {
  ArrowDownTrayIcon,
  CheckBadgeIcon,
  DocumentTextIcon,
} from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { formatDateTime } from '../../lib/format'
import { artifactDownloadUrl, availableFormats, downloadUrl } from '../../lib/download'
import { useArtifacts } from '../../api/pipeline'
import { useCaseSession } from '../../stores/sessionStore'
import { useUiStore } from '../../stores/uiStore'
import { VersionHistory, type VersionEntry } from '../document/VersionHistory'
import { StageCardShell, type StageCardProps } from '../pipeline/StageCardShell'
import type { DocVersionEvent, InteractionRequiredEvent } from '../../types/stream'

/** 交付文件行（合并 interaction 载荷 / SSE artifact_created / GET artifacts）。 */
interface DeliveryFile {
  artifactId: string
  kind: string
  filename: string
  createdAt?: string
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

function str(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

/** 文件扩展名（下载 chip 文案兜底）。 */
function extOf(filename: string): string {
  const dot = filename.lastIndexOf('.')
  return dot >= 0 ? filename.slice(dot + 1).toLowerCase() : ''
}

function labelOf(file: DeliveryFile): string {
  return (
    zh.stages.delivery.kindLabels[file.kind] ??
    zh.stages.delivery.kindLabels[extOf(file.filename)] ??
    file.filename
  )
}

function readPayloadFiles(payload: InteractionRequiredEvent | null): DeliveryFile[] {
  const def = asRecord(payload?.default)
  const raw = def?.files ?? def?.downloads
  if (!Array.isArray(raw)) return []
  const files: DeliveryFile[] = []
  for (const item of raw) {
    const record = asRecord(item)
    const artifactId = str(record?.artifact_id) || str(record?.id)
    if (!artifactId) continue
    files.push({
      artifactId,
      kind: str(record?.kind),
      filename: str(record?.filename) || artifactId,
      createdAt: str(record?.created_at) || undefined,
    })
  }
  return files
}

/** 版本事件 → VersionHistory 行（新→旧）。 */
function toVersionEntries(versions: DocVersionEvent[]): VersionEntry[] {
  return versions
    .map((v) => ({
      id: v.version_id,
      version: v.version,
      filename: v.filename,
      createdAt: v.created_at,
      iterationType: v.iteration_type ?? null,
      formats: availableFormats(v.downloads, v.filename),
    }))
    .reverse()
}

/**
 * §3.2 / §3.3 DeliveryCard：emerald 渐变成功面板 + 交付文件列表
 * （MD / DOCX / PDF / JSON 走 GET /api/v1/artifacts/{id}/download）+ 版本历史。
 */
export function DeliveryCard({ caseId, stage, submit, skip, busy }: StageCardProps) {
  const payload = stage.payload as InteractionRequiredEvent | null
  const session = useCaseSession(caseId)
  const pushToast = useUiStore((s) => s.pushToast)
  // 后端可用时补齐历史交付物；mock / 离线下失败不影响 SSE 已推送的清单
  const artifactsQuery = useArtifacts(caseId, undefined, stage.status === 'active')

  const files = useMemo<DeliveryFile[]>(() => {
    const merged = new Map<string, DeliveryFile>()
    for (const file of readPayloadFiles(payload)) merged.set(file.artifactId, file)
    for (const artifact of session?.artifacts ?? []) {
      merged.set(artifact.id, {
        artifactId: artifact.id,
        kind: artifact.kind,
        filename: artifact.filename,
        createdAt: artifact.created_at,
      })
    }
    for (const version of session?.versions ?? []) {
      for (const entry of version.downloads ?? []) {
        merged.set(entry.artifact_id, {
          artifactId: entry.artifact_id,
          kind: String(entry.kind),
          filename: entry.filename,
          createdAt: version.created_at,
        })
      }
    }
    for (const artifact of artifactsQuery.data ?? []) {
      if (!merged.has(artifact.id)) {
        merged.set(artifact.id, {
          artifactId: artifact.id,
          kind: artifact.kind,
          filename: artifact.filename,
          createdAt: artifact.created_at,
        })
      }
    }
    return [...merged.values()]
  }, [payload, session?.artifacts, session?.versions, artifactsQuery.data])

  const versions = useMemo(() => toVersionEntries(session?.versions ?? []), [session?.versions])

  /** 版本行下载：优先该版本 downloads 中的同格式条目。 */
  const downloadVersion = (entry: VersionEntry, format: string) => {
    const version = (session?.versions ?? []).find((v) => v.version_id === entry.id)
    const hit = version?.downloads?.find(
      (d) => String(d.kind).endsWith(format) || extOf(d.filename) === format,
    )
    const artifactId = hit?.artifact_id ?? (extOf(entry.filename) === format ? version?.artifact_id : undefined)
    if (!artifactId) {
      pushToast('info', zh.workspace.downloadUnavailable(format))
      return
    }
    downloadUrl(artifactDownloadUrl(artifactId), hit?.filename ?? entry.filename)
  }

  return (
    <StageCardShell
      stage={stage}
      icon={CheckBadgeIcon}
      summary={zh.stages.delivery.summary}
      onConfirm={() => submit({ confirmed: true })}
      onSkip={skip}
      confirmLabel={zh.stages.delivery.done}
      hideSkip
      busy={busy}
    >
      <div className="space-y-4">
        {/* 成功面板 */}
        <div
          className={cn(
            'rounded-xl border border-emerald-200/70 dark:border-emerald-500/30 p-4',
            'bg-gradient-to-br from-emerald-50 to-teal-50 dark:from-emerald-500/10 dark:to-teal-500/10',
            'flex items-start gap-3',
          )}
        >
          <span className="w-8 h-8 shrink-0 rounded-lg bg-emerald-500 text-white flex items-center justify-center">
            <CheckBadgeIcon className="w-5 h-5" strokeWidth={1.5} />
          </span>
          <div className="min-w-0">
            <p className="text-sm font-medium text-emerald-800 dark:text-emerald-300">
              {zh.stages.delivery.successTitle}
            </p>
            <p className="text-xs text-emerald-700/80 dark:text-emerald-400/80 mt-0.5">
              {payload?.prompt || zh.stages.delivery.successDesc}
            </p>
          </div>
        </div>

        {/* 交付文件 */}
        <div className="space-y-1.5">
          <p className="text-xs font-medium text-gray-500 dark:text-gray-400">
            {zh.stages.delivery.filesTitle}
          </p>
          {files.length === 0 ? (
            <p className="text-sm text-gray-400 dark:text-gray-500">
              {zh.stages.delivery.noFiles}
            </p>
          ) : (
            files.map((file) => (
              <div
                key={file.artifactId}
                className="flex items-center gap-2.5 rounded-xl border border-gray-200 dark:border-gray-700 px-3 py-2.5"
              >
                <span className="w-7 h-7 shrink-0 rounded bg-gray-100 dark:bg-gray-700 flex items-center justify-center">
                  <DocumentTextIcon
                    className="w-4 h-4 text-gray-500 dark:text-gray-300"
                    strokeWidth={1.5}
                  />
                </span>
                <div className="min-w-0 flex-1">
                  <p className="text-xs font-medium text-gray-800 dark:text-gray-100 truncate">
                    {labelOf(file)}
                  </p>
                  <p className="text-[11px] text-gray-400 dark:text-gray-500 truncate">
                    {file.filename}
                    {file.createdAt ? ` · ${formatDateTime(file.createdAt)}` : ''}
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() =>
                    downloadUrl(artifactDownloadUrl(file.artifactId), file.filename)
                  }
                  className={cn(
                    'shrink-0 inline-flex items-center gap-1 px-2 py-1 rounded-md border',
                    'border-gray-200 dark:border-gray-600 text-[11px] font-medium',
                    'text-gray-600 dark:text-gray-300 bg-white dark:bg-gray-800 transition-colors',
                    'hover:border-indigo-400/60 hover:text-[#6366f1] dark:hover:text-indigo-400',
                  )}
                >
                  <ArrowDownTrayIcon className="w-3 h-3" strokeWidth={1.5} />
                  {zh.document.formats[extOf(file.filename) as 'md' | 'docx' | 'pdf' | 'json'] ??
                    zh.stages.delivery.downloadLabel}
                </button>
              </div>
            ))
          )}
        </div>

        {/* 版本历史 */}
        {versions.length > 0 && (
          <div className="space-y-1.5">
            <p className="text-xs font-medium text-gray-500 dark:text-gray-400">
              {zh.stages.delivery.versionsTitle}
            </p>
            <VersionHistory versions={versions} onDownload={downloadVersion} />
          </div>
        )}
      </div>
    </StageCardShell>
  )
}
