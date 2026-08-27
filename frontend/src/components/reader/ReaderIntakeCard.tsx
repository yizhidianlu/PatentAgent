import { useCallback, useState } from 'react'
import { MagnifyingGlassIcon } from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { isLikelyPubNo, normalizePubNo } from '../../api/reader'
import { useStartPipeline } from '../../api/pipeline'
import { uploadCaseFile } from '../../api/uploads'
import { useUiStore } from '../../stores/uiStore'
import { Button } from '../ui/Button'
import { Card } from '../ui/Card'
import { Input } from '../ui/Input'
import { Spinner } from '../ui/Spinner'
import { Dropzone } from '../upload/Dropzone'

export interface ReaderIntakeCardProps {
  caseId: string
  className?: string
}

/**
 * §3.4 空会话态「输入来源」卡：
 * 公开号 Input +「开始解读」→ POST /pipeline/start {pub_no}；
 * 分隔线「或」；Dropzone(.pdf) → 先上传文件再 POST /pipeline/start {file_id}。
 */
export function ReaderIntakeCard({ caseId, className }: ReaderIntakeCardProps) {
  const pushToast = useUiStore((s) => s.pushToast)
  const startPipeline = useStartPipeline(caseId)
  const [pubNo, setPubNo] = useState('')
  const [busy, setBusy] = useState(false)
  const [uploading, setUploading] = useState(false)

  const startWith = useCallback(
    async (payload: Record<string, unknown>) => {
      setBusy(true)
      try {
        await startPipeline.mutateAsync(payload)
      } catch (e) {
        const message = e instanceof Error ? e.message : ''
        pushToast('error', message || zh.reader.startFailed)
      } finally {
        setBusy(false)
      }
    },
    [pushToast, startPipeline],
  )

  const handleStart = useCallback(() => {
    const value = normalizePubNo(pubNo)
    if (!value) {
      pushToast('warning', zh.reader.pubNoRequired)
      return
    }
    if (!isLikelyPubNo(value)) {
      pushToast('warning', zh.reader.pubNoInvalid)
      return
    }
    void startWith({ pub_no: value, source: 'pub_no' })
  }, [pubNo, pushToast, startWith])

  const handleFiles = useCallback(
    async (files: File[]) => {
      const file = files[0]
      if (!file || uploading || busy) return
      setUploading(true)
      try {
        const uploaded = await uploadCaseFile(caseId, file)
        pushToast('info', zh.reader.uploadStarted)
        await startWith({ file_id: uploaded.id, file_ids: [uploaded.id], source: 'pdf' })
      } catch (e) {
        const message = e instanceof Error ? e.message : ''
        pushToast('error', message || zh.reader.uploadFailed)
      } finally {
        setUploading(false)
      }
    },
    [busy, caseId, pushToast, startWith, uploading],
  )

  return (
    <div className={cn('pt-10 flex flex-col items-center text-center gap-4', className)}>
      <span className="w-14 h-14 rounded-2xl bg-indigo-50 dark:bg-indigo-500/10 flex items-center justify-center">
        <MagnifyingGlassIcon className="w-7 h-7 text-[#6366f1]" strokeWidth={1.5} />
      </span>
      <div className="space-y-1.5">
        <h2 className="text-xl sm:text-2xl font-bold tracking-tight text-gray-900 dark:text-gray-100">
          {zh.reader.emptyTitle}
        </h2>
        <p className="text-sm text-gray-500 dark:text-gray-400 max-w-md">{zh.reader.emptyDesc}</p>
      </div>

      <Card className="w-full max-w-lg p-5 sm:p-6 text-left space-y-4">
        <div className="space-y-2">
          <label
            htmlFor="reader-pub-no"
            className="block text-[13px] font-medium text-gray-700 dark:text-gray-200"
          >
            {zh.reader.pubNoLabel}
          </label>
          <div className="flex items-center gap-2">
            <Input
              id="reader-pub-no"
              value={pubNo}
              onChange={(e) => setPubNo(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault()
                  handleStart()
                }
              }}
              placeholder={zh.reader.pubNoPlaceholder}
              disabled={busy || uploading}
              className="flex-1 min-w-0"
            />
            <Button
              onClick={handleStart}
              disabled={busy || uploading}
              className="shrink-0 gap-1.5"
            >
              {busy && !uploading && <Spinner size="sm" className="border-white/40 border-t-white" />}
              {zh.reader.start}
            </Button>
          </div>
        </div>

        {/* 分隔线「或」 */}
        <div className="flex items-center gap-3 text-xs text-gray-400 dark:text-gray-500">
          <span className="h-px flex-1 bg-gray-200 dark:bg-gray-700" aria-hidden="true" />
          {zh.reader.or}
          <span className="h-px flex-1 bg-gray-200 dark:bg-gray-700" aria-hidden="true" />
        </div>

        <Dropzone
          accept=".pdf,application/pdf"
          multiple={false}
          title={zh.reader.dropHint}
          hint={zh.reader.dropAccept}
          disabled={busy || uploading}
          onFiles={(files) => void handleFiles(files)}
        />

        {uploading && (
          <span className="flex items-center justify-center gap-2 text-xs text-gray-500 dark:text-gray-400">
            <Spinner size="sm" />
            {zh.reader.uploading}
          </span>
        )}
      </Card>
    </div>
  )
}
