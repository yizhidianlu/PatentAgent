import { useState } from 'react'
import { ArrowUpTrayIcon, DocumentTextIcon } from '@heroicons/react/24/outline'
import { zh } from '../../i18n/zh'
import { formatFileSize } from '../../lib/format'
import { uploadCaseFiles, type UploadedFile } from '../../api/uploads'
import { useUiStore } from '../../stores/uiStore'
import { Dropzone } from '../upload/Dropzone'
import { Spinner } from '../ui/Spinner'
import { StageCardShell, type StageCardProps } from '../pipeline/StageCardShell'
import type { InteractionRequiredEvent } from '../../types/stream'

/** §4 MaterialUploadCard：Dropzone + 已传文件列表 + 确认（submit {file_ids}）。 */
export function MaterialUploadCard({ caseId, stage, submit, skip, busy }: StageCardProps) {
  const payload = stage.payload as InteractionRequiredEvent | null
  const pushToast = useUiStore((s) => s.pushToast)
  const [files, setFiles] = useState<UploadedFile[]>([])
  const [uploading, setUploading] = useState(false)

  const resultIds = (stage.result as { file_ids?: string[] } | undefined)?.file_ids

  const handleFiles = async (picked: File[]) => {
    if (picked.length === 0 || uploading) return
    setUploading(true)
    try {
      const uploaded = await uploadCaseFiles(caseId, picked)
      setFiles((prev) => [...prev, ...uploaded])
    } catch (e) {
      const message = e instanceof Error ? e.message : zh.stages.materialUpload.uploadFailed
      pushToast('error', `${zh.stages.materialUpload.uploadFailed}：${message}`)
    } finally {
      setUploading(false)
    }
  }

  return (
    <StageCardShell
      stage={stage}
      icon={ArrowUpTrayIcon}
      summary={zh.stages.materialUpload.summary(resultIds?.length ?? files.length)}
      onConfirm={() => submit({ file_ids: files.map((f) => f.id) })}
      onSkip={skip}
      confirmLabel={zh.stages.materialUpload.confirm}
      confirmDisabled={uploading}
      busy={busy}
    >
      <div className="space-y-3">
        {payload?.prompt && (
          <p className="text-sm text-gray-700 dark:text-gray-300">{payload.prompt}</p>
        )}

        <Dropzone onFiles={(picked) => void handleFiles(picked)} disabled={uploading} />

        {uploading && (
          <div className="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
            <Spinner size="sm" />
            {zh.composer.uploading}
          </div>
        )}

        {files.length > 0 && (
          <div className="space-y-1.5">
            <p className="text-xs font-medium text-gray-500 dark:text-gray-400">
              {zh.stages.materialUpload.listTitle}
            </p>
            {files.map((file) => (
              <div
                key={file.id}
                className="flex items-center gap-2.5 rounded-lg border border-gray-200 dark:border-gray-700 px-3 py-2"
              >
                <span className="w-7 h-7 rounded bg-gray-100 dark:bg-gray-700 flex items-center justify-center shrink-0">
                  <DocumentTextIcon className="w-4 h-4 text-gray-500 dark:text-gray-300" strokeWidth={1.5} />
                </span>
                <span className="text-xs font-medium text-gray-800 dark:text-gray-100 truncate flex-1 min-w-0">
                  {file.orig_name}
                </span>
                {file.size !== null && (
                  <span className="text-[10px] text-gray-400 shrink-0">
                    {formatFileSize(file.size)}
                  </span>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </StageCardShell>
  )
}
