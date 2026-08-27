import { useMemo, useState } from 'react'
import {
  ExclamationTriangleIcon,
  PencilSquareIcon,
  ScaleIcon,
  TrashIcon,
} from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { Badge } from '../ui/Badge'
import { Input } from '../ui/Input'
import { Select } from '../ui/Select'
import { StageCardShell, type StageCardProps } from '../pipeline/StageCardShell'
import { asArray, asRecord, num, pickArray, pickStr, readDefault, str } from './stagePayload'
import type { InteractionRequiredEvent } from '../../types/stream'

/** 对比文件（NoticeIssue.cited_documents[]）。 */
interface CitedDoc {
  code: string
  refLabel: string
  identifier: string
  usedAgainst: number[]
}

/** 一条审查问题（NoticeIssue）。 */
interface Issue {
  id: string
  statute: string
  defectType: string
  inferred: boolean
  claims: number[]
  digest: string
  cited: CitedDoc[]
  note: string
  drop: boolean
}

interface DefectOption {
  value: string
  label: string
}

function toIntList(value: unknown): number[] {
  return asArray(value)
    .map((v) => num(v))
    .filter((v): v is number => v !== null && Number.isFinite(v))
    .map((v) => Math.trunc(v))
}

function readIssues(defaults: Record<string, unknown>): Issue[] {
  return pickArray(defaults, 'issues').map((raw, i) => {
    const record = asRecord(raw)
    return {
      id: pickStr(record, 'id') || `I${i + 1}`,
      statute: pickStr(record, 'statute'),
      defectType: pickStr(record, 'defect_type'),
      inferred: record?.inferred === true,
      claims: toIntList(record?.claims_involved),
      digest: pickStr(record, 'examiner_argument_digest', 'digest'),
      cited: pickArray(record, 'cited_documents').map((doc) => {
        const d = asRecord(doc)
        return {
          code: pickStr(d, 'code') || pickStr(d, 'ref_label'),
          refLabel: pickStr(d, 'ref_label'),
          identifier: pickStr(d, 'identifier', 'pub_no', 'pub_number'),
          usedAgainst: toIntList(d?.used_against),
        }
      }),
      note: pickStr(record, 'note'),
      drop: false,
    }
  })
}

function readDefectOptions(defaults: Record<string, unknown>): DefectOption[] {
  const raw = pickArray(defaults, 'defect_type_options')
  const fromPayload = raw
    .map((item) => {
      const record = asRecord(item)
      const value = pickStr(record, 'value') || str(item)
      return { value, label: pickStr(record, 'label') || zh.oa.labels.defectTypes[value] || value }
    })
    .filter((o) => o.value)
  if (fromPayload.length > 0) return fromPayload
  return Object.entries(zh.oa.labels.defectTypes).map(([value, label]) => ({ value, label }))
}

/** 从权利要求书原文里数出权项号（「1. …」「权利要求 3」两种写法）。 */
function parseClaimNumbers(text: string): number[] {
  const found = new Set<number>()
  for (const m of text.matchAll(/^\s*(\d{1,3})\s*[.、．]/gm)) found.add(Number(m[1]))
  for (const m of text.matchAll(/权利要求\s*(\d{1,3})/g)) found.add(Number(m[1]))
  return [...found].filter((n) => n > 0 && n < 200).sort((a, b) => a - b)
}

function claimsSummary(claims: number[]): string {
  if (claims.length === 0) return zh.oa.issues.noClaims
  return claims.map((n) => zh.oa.issues.claimChip(n)).join('、')
}

/**
 * §3.5 OaIssuesCard（kind='oa_issues'，后端 P1 强制人审闸门）：
 * 「共识别 N 个审查问题」+ 逐条问题行（序号 + 条款红徽章 + 涉及权项 chips +
 * 审查员观点 line-clamp-2 可展开 + 对比文件 D1/D2 标签），
 * 每条可展开编辑（法条文本、缺陷类型下拉、涉及权项多选、观点摘要、删除本条），
 * 未识别到本申请权项时前置权利要求书原文输入框。
 * 提交格式对齐 `pipelines/oa.py::_apply_issue_edits`。
 */
export function OaIssuesCard({ stage, submit, skip, busy }: StageCardProps) {
  const payload = stage.payload as InteractionRequiredEvent | null
  const defaults = useMemo(() => readDefault(payload), [payload])

  const [issues, setIssues] = useState<Issue[]>(() => readIssues(defaults))
  const [claimsText, setClaimsText] = useState(() => pickStr(defaults, 'claims_text'))
  const [expanded, setExpanded] = useState<string[]>([])
  const [editing, setEditing] = useState<string[]>([])

  const defectOptions = useMemo(() => readDefectOptions(defaults), [defaults])
  const lintProblems = useMemo(
    () => pickArray(defaults, 'lint_problems').map(str).filter(Boolean),
    [defaults],
  )

  const noticeKind = pickStr(defaults, 'notice_kind')
  const noticeNo = num(defaults.notice_no)
  const applicationNo = pickStr(defaults, 'application_no')
  const patentType = pickStr(defaults, 'patent_type')
  const deadline = pickStr(defaults, 'deadline')

  /** 可勾选的权项号：权利要求书原文解析 + 各条已涉及权项的并集。 */
  const claimOptions = useMemo(() => {
    const found = new Set<number>(parseClaimNumbers(claimsText))
    for (const issue of issues) for (const n of issue.claims) found.add(n)
    if (found.size === 0) return []
    const max = Math.max(...found)
    return Array.from({ length: Math.min(max, 40) }, (_, i) => i + 1)
  }, [claimsText, issues])

  const patch = (id: string, next: Partial<Issue>): void => {
    setIssues((prev) => prev.map((item) => (item.id === id ? { ...item, ...next } : item)))
  }

  const toggleIn = (list: string[], id: string): string[] =>
    list.includes(id) ? list.filter((x) => x !== id) : [...list, id]

  const toggleClaim = (issue: Issue, no: number): void => {
    const next = issue.claims.includes(no)
      ? issue.claims.filter((n) => n !== no)
      : [...issue.claims, no].sort((a, b) => a - b)
    patch(issue.id, { claims: next })
  }

  const kept = issues.filter((i) => !i.drop)

  const handleConfirm = (): void => {
    submit({
      notice_kind: noticeKind,
      notice_no: noticeNo,
      application_no: applicationNo,
      patent_type: patentType,
      deadline,
      claims_text: claimsText,
      issues: issues.map((issue) => ({
        id: issue.id,
        statute: issue.statute,
        defect_type: issue.defectType,
        claims_involved: issue.claims,
        examiner_argument_digest: issue.digest,
        note: issue.note,
        drop: issue.drop,
      })),
    })
  }

  return (
    <StageCardShell
      stage={stage}
      icon={ScaleIcon}
      title={zh.oa.issues.title}
      summary={zh.oa.issues.summary(kept.length)}
      onConfirm={handleConfirm}
      onSkip={skip}
      confirmLabel={zh.oa.issues.confirm}
      busy={busy}
    >
      <div className="space-y-3">
        {payload?.prompt && (
          <p className="text-sm text-gray-700 dark:text-gray-300 whitespace-pre-wrap">
            {payload.prompt}
          </p>
        )}

        {/* 通知书著录项 */}
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-xs text-gray-500 dark:text-gray-400">
          <span className="text-[13px] font-medium text-gray-900 dark:text-gray-100">
            {zh.oa.issues.count(kept.length)}
          </span>
          {noticeKind && (
            <span>
              {zh.oa.issues.meta.noticeKind}：
              {zh.oa.labels.noticeKinds[noticeKind] ?? noticeKind}
              {noticeNo ? ` · 第 ${noticeNo} 次` : ''}
            </span>
          )}
          {applicationNo && <span>{zh.oa.issues.meta.applicationNo}：{applicationNo}</span>}
          {patentType && (
            <span>
              {zh.oa.issues.meta.patentType}：
              {zh.oa.labels.patentTypes[patentType] ?? patentType}
            </span>
          )}
          {deadline && <span>{zh.oa.issues.meta.deadline}：{deadline}</span>}
        </div>

        {/* 服务端枚举校验提示 */}
        {lintProblems.length > 0 && (
          <div className="rounded-xl border border-amber-200 dark:border-amber-500/30 bg-amber-50 dark:bg-amber-500/10 px-3 py-2.5">
            <p className="flex items-center gap-1.5 text-xs font-medium text-amber-700 dark:text-amber-400">
              <ExclamationTriangleIcon className="w-4 h-4 shrink-0" strokeWidth={1.5} />
              {zh.oa.issues.lintTitle}
            </p>
            <ul className="mt-1 space-y-0.5 text-xs text-amber-700/90 dark:text-amber-400/90 list-disc pl-5">
              {lintProblems.map((problem, i) => (
                <li key={i}>{problem}</li>
              ))}
            </ul>
          </div>
        )}

        {issues.length === 0 ? (
          <p className="text-sm text-gray-400 dark:text-gray-500">{zh.oa.issues.empty}</p>
        ) : (
          <div className="space-y-2">
            {issues.map((issue, index) => {
              const isExpanded = expanded.includes(issue.id)
              const isEditing = editing.includes(issue.id)
              const defectLabel =
                zh.oa.labels.defectTypes[issue.defectType] ?? issue.defectType ?? ''
              return (
                <div
                  key={issue.id}
                  className={cn(
                    'rounded-xl border p-3 space-y-2 transition-colors',
                    issue.drop
                      ? 'border-gray-200 dark:border-gray-700 opacity-50'
                      : 'border-gray-200 dark:border-gray-700',
                  )}
                >
                  {/* 头行：序号 + 条款红徽章 + 涉及权项 chips */}
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="w-5 h-5 shrink-0 rounded-md bg-gray-100 dark:bg-gray-700 text-[11px] font-semibold text-gray-500 dark:text-gray-300 flex items-center justify-center">
                      {index + 1}
                    </span>
                    <Badge variant="red">
                      {[issue.statute, defectLabel].filter(Boolean).join(' · ') || defectLabel}
                    </Badge>
                    {issue.inferred && <Badge variant="amber">{zh.oa.issues.inferred}</Badge>}
                    <span className="text-[11px] text-gray-400 dark:text-gray-500">
                      {zh.oa.issues.claimsLabel}：
                    </span>
                    {issue.claims.length === 0 ? (
                      <span className="text-[11px] text-gray-400 dark:text-gray-500">
                        {zh.oa.issues.noClaims}
                      </span>
                    ) : (
                      issue.claims.map((no) => (
                        <span
                          key={no}
                          className="px-1.5 py-0.5 rounded-full bg-indigo-50 dark:bg-indigo-500/10 text-[10px] font-medium text-[#6366f1] dark:text-indigo-400"
                        >
                          {zh.oa.issues.claimChip(no)}
                        </span>
                      ))
                    )}
                    <span className="ml-auto flex items-center gap-1">
                      <button
                        type="button"
                        onClick={() => setEditing((prev) => toggleIn(prev, issue.id))}
                        className="inline-flex items-center gap-1 px-2 py-1 rounded-lg text-[11px] text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
                      >
                        <PencilSquareIcon className="w-3.5 h-3.5" strokeWidth={1.5} />
                        {isEditing ? zh.oa.issues.editDone : zh.oa.issues.edit}
                      </button>
                      <button
                        type="button"
                        onClick={() => patch(issue.id, { drop: !issue.drop })}
                        aria-label={issue.drop ? zh.oa.issues.undrop : zh.oa.issues.drop}
                        title={issue.drop ? zh.oa.issues.undrop : zh.oa.issues.drop}
                        className="inline-flex items-center gap-1 px-2 py-1 rounded-lg text-[11px] text-gray-400 hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-500/10 transition-colors"
                      >
                        <TrashIcon className="w-3.5 h-3.5" strokeWidth={1.5} />
                        {issue.drop ? zh.oa.issues.dropped : ''}
                      </button>
                    </span>
                  </div>

                  {/* 审查员观点摘录（line-clamp-2 可展开） */}
                  {issue.digest && (
                    <div className="text-[13px] leading-relaxed text-gray-600 dark:text-gray-300">
                      <span className="text-gray-400 dark:text-gray-500">
                        {zh.oa.issues.digestLabel}：
                      </span>
                      <span className={cn(!isExpanded && 'line-clamp-2')}>{issue.digest}</span>
                      <button
                        type="button"
                        onClick={() => setExpanded((prev) => toggleIn(prev, issue.id))}
                        className="ml-1 text-[11px] text-[#6366f1] hover:underline"
                      >
                        {isExpanded ? zh.oa.issues.collapse : zh.oa.issues.expand}
                      </button>
                    </div>
                  )}

                  {/* 对比文件 D1 / D2 */}
                  {issue.cited.length > 0 && (
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span className="text-[11px] text-gray-400 dark:text-gray-500">
                        {zh.oa.issues.citedLabel}：
                      </span>
                      {issue.cited.map((doc, i) => (
                        <span
                          key={`${doc.code}-${i}`}
                          className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md border border-gray-200 dark:border-gray-600 text-[10px] text-gray-600 dark:text-gray-300"
                        >
                          <span className="font-semibold text-gray-900 dark:text-gray-100">
                            {doc.code || doc.refLabel}
                          </span>
                          {doc.identifier && <span>{doc.identifier}</span>}
                          {doc.usedAgainst.length > 0 && (
                            <span className="text-gray-400">
                              → {claimsSummary(doc.usedAgainst)}
                            </span>
                          )}
                        </span>
                      ))}
                    </div>
                  )}

                  {/* 逐条编辑 */}
                  {isEditing && (
                    <div className="space-y-2 pt-2 border-t border-gray-100 dark:border-gray-700">
                      <div className="grid sm:grid-cols-2 gap-2">
                        <label className="space-y-1">
                          <span className="block text-[11px] text-gray-500 dark:text-gray-400">
                            {zh.oa.issues.statuteLabel}
                          </span>
                          <Input
                            value={issue.statute}
                            placeholder={zh.oa.issues.statutePlaceholder}
                            onChange={(e) => patch(issue.id, { statute: e.target.value })}
                            className="h-9 text-[13px]"
                          />
                        </label>
                        <label className="space-y-1">
                          <span className="block text-[11px] text-gray-500 dark:text-gray-400">
                            {zh.oa.issues.defectLabel}
                          </span>
                          <Select
                            value={issue.defectType}
                            onChange={(e) => patch(issue.id, { defectType: e.target.value })}
                          >
                            {defectOptions.map((option) => (
                              <option key={option.value} value={option.value}>
                                {option.label}
                              </option>
                            ))}
                          </Select>
                        </label>
                      </div>

                      {claimOptions.length > 0 && (
                        <div className="space-y-1">
                          <span className="block text-[11px] text-gray-500 dark:text-gray-400">
                            {zh.oa.issues.claimsPick}
                          </span>
                          <div className="flex flex-wrap gap-1.5">
                            {claimOptions.map((no) => {
                              const active = issue.claims.includes(no)
                              return (
                                <button
                                  key={no}
                                  type="button"
                                  onClick={() => toggleClaim(issue, no)}
                                  className={cn(
                                    'px-2 py-0.5 rounded-full text-[11px] border transition-colors',
                                    active
                                      ? 'border-[#6366f1] bg-indigo-50 dark:bg-indigo-500/10 text-[#6366f1] dark:text-indigo-400'
                                      : 'border-gray-200 dark:border-gray-600 text-gray-500 dark:text-gray-400 hover:border-indigo-300',
                                  )}
                                >
                                  {zh.oa.issues.claimChip(no)}
                                </button>
                              )
                            })}
                          </div>
                        </div>
                      )}

                      <label className="block space-y-1">
                        <span className="block text-[11px] text-gray-500 dark:text-gray-400">
                          {zh.oa.issues.digestEdit}
                        </span>
                        <textarea
                          value={issue.digest}
                          rows={3}
                          onChange={(e) => patch(issue.id, { digest: e.target.value })}
                          className="w-full rounded-xl border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 px-3 py-2 text-[13px] text-gray-900 dark:text-gray-100 outline-none focus:border-[#6366f1] focus:ring-4 focus:ring-indigo-500/10 resize-y"
                        />
                      </label>
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        )}

        {/* 本申请权利要求书原文（缺失时后端会在 prompt 中要求补录） */}
        <label className="block space-y-1">
          <span className="block text-[13px] font-medium text-gray-700 dark:text-gray-200">
            {zh.oa.issues.claimsTextLabel}
          </span>
          <span className="block text-[11px] text-gray-400 dark:text-gray-500">
            {zh.oa.issues.claimsTextHint}
          </span>
          <textarea
            value={claimsText}
            rows={claimsText ? 4 : 3}
            placeholder={zh.oa.issues.claimsTextPlaceholder}
            onChange={(e) => setClaimsText(e.target.value)}
            className="w-full rounded-xl border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 px-3 py-2 text-[13px] leading-relaxed text-gray-900 dark:text-gray-100 outline-none focus:border-[#6366f1] focus:ring-4 focus:ring-indigo-500/10 resize-y"
          />
        </label>
      </div>
    </StageCardShell>
  )
}
