import { useState } from 'react'
import { ArrowUpTrayIcon } from '@heroicons/react/24/outline'
import { zh } from '../../i18n/zh'
import { SelectedFiles } from '../upload/SelectedFiles'
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

        {/* 与导入弹窗共用同一套样式：两处分头写过一次，结果一处有图标一处是裸文本 */}
        <SelectedFiles
          title={zh.stages.materialUpload.listTitle}
          files={files.map((f) => ({ name: f.orig_name, size: f.size }))}
        />
      </div>
    </StageCardShell>
  )
}
