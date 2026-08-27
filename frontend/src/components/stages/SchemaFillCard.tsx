import { useMemo, useState } from 'react'
import {
  ClipboardDocumentIcon,
  PlusIcon,
  TableCellsIcon,
  TrashIcon,
} from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { artifactDownloadUrl, fileDownloadUrl } from '../../lib/download'
import { useUiStore } from '../../stores/uiStore'
import { Badge } from '../ui/Badge'
import { Input } from '../ui/Input'
import { StageCardShell, type StageCardProps } from '../pipeline/StageCardShell'
import { asRecord, num, pickArray, pickStr, readDefault, str, strList } from './stagePayload'
import type { InteractionRequiredEvent } from '../../types/stream'

// ---------------------------------------------------------------------------
// 载荷读取（models/disclosure.py：StructureSchema / AppearanceSchema / FigurePlan）
// ---------------------------------------------------------------------------

interface Part {
  id: string
  name: string
  shape: string
  material: string
}

interface Relation {
  from: string
  to: string
  type: string
  where: string
}

interface OmittedView {
  name: string
  reason: string
}

interface Figure {
  key: string
  fig: number | null
  role: string
  kind: string
  path: string
  url: string | null
  score: number
  reason: string
  use: boolean
}

interface BriefView {
  name: string
  goal: string
  prompt: string
  hint: string
}

/** 外观设计常用视图面（与勾选态并集渲染）。 */
const DEFAULT_FACES = ['主视图', '后视图', '左视图', '右视图', '俯视图', '仰视图', '立体图']

/** 事实合同载荷可能挂在 default 的多个键上，逐个尝试。 */
function readSchemaRecord(defaults: Record<string, unknown>): Record<string, unknown> {
  for (const key of ['schema', 'structure', 'appearance', 'structure_schema', 'appearance_schema']) {
    const record = asRecord(defaults[key])
    if (record) return record
  }
  return defaults
}

function readFigurePlan(defaults: Record<string, unknown>): Record<string, unknown> {
  const record = asRecord(defaults.figure_plan) ?? asRecord(defaults.plan)
  return record ?? defaults
}

function readParts(schema: Record<string, unknown>): Part[] {
  return pickArray(schema, 'parts').map((raw, i) => {
    const record = asRecord(raw)
    return {
      id: pickStr(record, 'id') || `P${i + 1}`,
      name: pickStr(record, 'name'),
      shape: pickStr(record, 'shape'),
      material: pickStr(record, 'material_hint', 'material'),
    }
  })
}

function readRelations(schema: Record<string, unknown>): Relation[] {
  return pickArray(schema, 'relations').map((raw) => {
    const record = asRecord(raw)
    return {
      from: pickStr(record, 'from', 'from_'),
      to: pickStr(record, 'to'),
      type: pickStr(record, 'type'),
      where: pickStr(record, 'where'),
    }
  })
}

function readFigures(plan: Record<string, unknown>): Figure[] {
  return pickArray(plan, 'figures', 'items').map((raw, i) => {
    const record = asRecord(raw)
    const path = pickStr(record, 'path', 'stored_path', 'filename')
    const direct = pickStr(record, 'url', 'src', 'thumb_url')
    const artifactId = pickStr(record, 'artifact_id')
    const fileId = pickStr(record, 'file_id')
    const kind = pickStr(record, 'kind') || 'other'
    return {
      key: `${path || 'fig'}-${i}`,
      fig: num(record?.fig),
      role: pickStr(record, 'role') || 'reference',
      kind,
      path,
      url:
        direct ||
        (artifactId ? artifactDownloadUrl(artifactId) : fileId ? fileDownloadUrl(fileId) : null),
      score: num(record?.score) ?? 0,
      reason: pickStr(record, 'reason'),
      // CAD 一律不入文（figure_plan.schema.yaml 硬规则）
      use: kind === 'cad' ? false : record?.use_in_disclosure === true,
    }
  })
}

function readBriefViews(brief: Record<string, unknown> | null): BriefView[] {
  return pickArray(brief, 'views').map((raw) => {
    const record = asRecord(raw)
    return {
      name: pickStr(record, 'view_name', 'name'),
      goal: pickStr(record, 'lineart_goal', 'goal'),
      prompt: pickStr(record, 'gen_prompt', 'prompt'),
      hint: pickStr(record, 'output_hint', 'hint'),
    }
  })
}

/** 「线稿绘制说明」的可复制纯文本。 */
function briefText(views: BriefView[], forbid: string[], note: string): string {
  const lines: string[] = [zh.schemaFill.briefTitle, '']
  views.forEach((view, i) => {
    lines.push(`${i + 1}. ${view.name}`)
    if (view.goal) lines.push(`   目标：${view.goal}`)
    if (view.prompt) lines.push(`   绘制要点：${view.prompt}`)
    if (view.hint) lines.push(`   交付：${view.hint}`)
  })
  if (forbid.length > 0) lines.push('', `${zh.schemaFill.briefForbid}：${forbid.join('；')}`)
  if (note) lines.push('', note)
  return lines.join('\n')
}

// ---------------------------------------------------------------------------
// 卡片
// ---------------------------------------------------------------------------

/** 小号文本输入（表格单元格）。 */
function CellInput(props: { value: string; onChange: (v: string) => void; placeholder?: string }) {
  return (
    <Input
      value={props.value}
      placeholder={props.placeholder}
      onChange={(e) => props.onChange(e.target.value)}
      className="h-8 px-2 text-[12px] rounded-lg"
    />
  )
}

function RemoveButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={zh.schemaFill.remove}
      title={zh.schemaFill.remove}
      className="w-7 h-7 shrink-0 rounded-lg flex items-center justify-center text-gray-400 hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-500/10 transition-colors"
    >
      <TrashIcon className="w-3.5 h-3.5" strokeWidth={1.5} />
    </button>
  )
}

function AddButton({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg border border-dashed border-gray-300 dark:border-gray-600 text-[11px] text-gray-500 dark:text-gray-400 hover:border-[#6366f1] hover:text-[#6366f1] transition-colors"
    >
      <PlusIcon className="w-3.5 h-3.5" strokeWidth={2} />
      {label}
    </button>
  )
}

/**
 * A3b SchemaFillCard（kind='schema_fill'）：实用新型 / 外观设计的「填表与线稿」。
 * - 实用新型：部件表（名称 / 形状 / 材料，可增删改）+ 连接关系表（A/B/类型/位置）；
 * - 外观设计：产品形态（立体 / 平面）+ 主张视图面多选 + 省略视图说明 + 设计要点；
 * - 两者共用：figure_plan 图片网格（缩略图 + role/kind 徽章 + score + 「入文」开关，
 *   CAD 项禁用并标注「不入文」）；缺合格线稿时展示可复制的「线稿绘制说明」。
 */
export function SchemaFillCard({ stage, submit, skip, busy }: StageCardProps) {
  const payload = stage.payload as InteractionRequiredEvent | null
  const defaults = useMemo(() => readDefault(payload), [payload])
  const pushToast = useUiStore((s) => s.pushToast)

  const schemaRecord = useMemo(() => readSchemaRecord(defaults), [defaults])
  const planRecord = useMemo(() => readFigurePlan(defaults), [defaults])
  const brief = useMemo(
    () => asRecord(defaults.lineart_brief) ?? asRecord(defaults.brief),
    [defaults],
  )

  const patentType =
    pickStr(defaults, 'patent_type') ||
    pickStr(schemaRecord, 'patent_type') ||
    pickStr(planRecord, 'patent_type') ||
    (pickArray(schemaRecord, 'parts').length > 0 ? 'utility_model' : 'design')
  const isDesign = patentType === 'design'

  const [parts, setParts] = useState<Part[]>(() => readParts(schemaRecord))
  const [relations, setRelations] = useState<Relation[]>(() => readRelations(schemaRecord))
  const [productForm, setProductForm] = useState<string>(
    () => pickStr(schemaRecord, 'product_form') || 'solid',
  )
  const [faces, setFaces] = useState<string[]>(() => strList(schemaRecord.claimed_faces))
  const [omitted, setOmitted] = useState<OmittedView[]>(() =>
    pickArray(schemaRecord, 'omitted_views').map((raw) => {
      const record = asRecord(raw)
      return { name: pickStr(record, 'name'), reason: pickStr(record, 'reason') }
    }),
  )
  const [designPoints, setDesignPoints] = useState<string[]>(() =>
    strList(schemaRecord.design_points),
  )
  const [figures, setFigures] = useState<Figure[]>(() => readFigures(planRecord))

  const faceOptions = useMemo(
    () => [...new Set([...DEFAULT_FACES, ...faces])],
    [faces],
  )

  const briefViews = useMemo(() => readBriefViews(brief), [brief])
  const briefForbid = useMemo(() => strList(brief?.forbid), [brief])
  const briefNote = pickStr(brief, 'note')
  const briefRisk = pickStr(brief, 'risk_note')
  const briefNeeded = brief?.needed !== false && briefViews.length > 0

  const usedCount = figures.filter((f) => f.use).length

  const copyBrief = async (): Promise<void> => {
    const text = briefText(briefViews, briefForbid, briefNote)
    try {
      await navigator.clipboard.writeText(text)
      pushToast('info', zh.schemaFill.briefCopied)
    } catch {
      pushToast('warning', zh.schemaFill.briefCopyFailed)
    }
  }

  const handleConfirm = (): void => {
    const schemaOut: Record<string, unknown> = { ...schemaRecord }
    if (isDesign) {
      schemaOut.product_form = productForm
      schemaOut.claimed_faces = faces
      schemaOut.omitted_views = omitted.filter((o) => o.name.trim() || o.reason.trim())
      schemaOut.design_points = designPoints.filter((p) => p.trim())
    } else {
      schemaOut.parts = parts
        .filter((p) => p.name.trim() || p.shape.trim() || p.material.trim())
        .map((p) => ({ id: p.id, name: p.name, shape: p.shape, material_hint: p.material }))
      schemaOut.relations = relations
        .filter((r) => r.from.trim() || r.to.trim())
        .map((r) => ({ from: r.from, to: r.to, type: r.type, where: r.where }))
    }
    submit({
      patent_type: patentType,
      schema: schemaOut,
      figure_plan: {
        ...planRecord,
        figures: figures.map((f) => ({
          fig: f.fig,
          path: f.path,
          role: f.role,
          kind: f.kind,
          score: f.score,
          use_in_disclosure: f.use,
        })),
      },
      skip: false,
    })
  }

  return (
    <StageCardShell
      stage={stage}
      icon={TableCellsIcon}
      title={zh.schemaFill.title}
      summary={zh.schemaFill.summary(isDesign ? designPoints.length : parts.length, usedCount)}
      onConfirm={handleConfirm}
      onSkip={skip}
      confirmLabel={zh.schemaFill.confirm}
      busy={busy}
    >
      <div className="space-y-4">
        {payload?.prompt && (
          <p className="text-sm text-gray-700 dark:text-gray-300 whitespace-pre-wrap">
            {payload.prompt}
          </p>
        )}

        {/* --- 实用新型：部件表 + 连接关系表 --- */}
        {!isDesign && (
          <>
            <section className="space-y-2">
              <div>
                <p className="text-[13px] font-medium text-gray-700 dark:text-gray-200">
                  {zh.schemaFill.partsTitle}
                </p>
                <p className="text-[11px] text-gray-400 dark:text-gray-500">
                  {zh.schemaFill.partsHint}
                </p>
              </div>
              <div className="hidden sm:grid grid-cols-[1fr_1.2fr_1fr_28px] gap-2 px-1 text-[11px] text-gray-400 dark:text-gray-500">
                <span>{zh.schemaFill.partName}</span>
                <span>{zh.schemaFill.partShape}</span>
                <span>{zh.schemaFill.partMaterial}</span>
                <span />
              </div>
              {parts.length === 0 ? (
                <p className="text-[12px] text-gray-400 dark:text-gray-500">
                  {zh.schemaFill.emptyRows}
                </p>
              ) : (
                <div className="space-y-1.5">
                  {parts.map((part, index) => (
                    <div
                      key={part.id}
                      className="grid grid-cols-1 sm:grid-cols-[1fr_1.2fr_1fr_28px] gap-2 items-center"
                    >
                      <CellInput
                        value={part.name}
                        placeholder={zh.schemaFill.partName}
                        onChange={(v) =>
                          setParts((prev) =>
                            prev.map((p, i) => (i === index ? { ...p, name: v } : p)),
                          )
                        }
                      />
                      <CellInput
                        value={part.shape}
                        placeholder={zh.schemaFill.partShape}
                        onChange={(v) =>
                          setParts((prev) =>
                            prev.map((p, i) => (i === index ? { ...p, shape: v } : p)),
                          )
                        }
                      />
                      <CellInput
                        value={part.material}
                        placeholder={zh.schemaFill.partMaterial}
                        onChange={(v) =>
                          setParts((prev) =>
                            prev.map((p, i) => (i === index ? { ...p, material: v } : p)),
                          )
                        }
                      />
                      <RemoveButton
                        onClick={() => setParts((prev) => prev.filter((_, i) => i !== index))}
                      />
                    </div>
                  ))}
                </div>
              )}
              <AddButton
                label={zh.schemaFill.addPart}
                onClick={() =>
                  setParts((prev) => [
                    ...prev,
                    { id: `P${prev.length + 1}`, name: '', shape: '', material: '' },
                  ])
                }
              />
            </section>

            <section className="space-y-2">
              <div>
                <p className="text-[13px] font-medium text-gray-700 dark:text-gray-200">
                  {zh.schemaFill.relationsTitle}
                </p>
                <p className="text-[11px] text-gray-400 dark:text-gray-500">
                  {zh.schemaFill.relationsHint}
                </p>
              </div>
              <div className="hidden sm:grid grid-cols-[1fr_1fr_1fr_1.2fr_28px] gap-2 px-1 text-[11px] text-gray-400 dark:text-gray-500">
                <span>{zh.schemaFill.relFrom}</span>
                <span>{zh.schemaFill.relTo}</span>
                <span>{zh.schemaFill.relType}</span>
                <span>{zh.schemaFill.relWhere}</span>
                <span />
              </div>
              {relations.length === 0 ? (
                <p className="text-[12px] text-gray-400 dark:text-gray-500">
                  {zh.schemaFill.emptyRows}
                </p>
              ) : (
                <div className="space-y-1.5">
                  {relations.map((relation, index) => (
                    <div
                      key={index}
                      className="grid grid-cols-1 sm:grid-cols-[1fr_1fr_1fr_1.2fr_28px] gap-2 items-center"
                    >
                      <CellInput
                        value={relation.from}
                        placeholder={zh.schemaFill.relFrom}
                        onChange={(v) =>
                          setRelations((prev) =>
                            prev.map((r, i) => (i === index ? { ...r, from: v } : r)),
                          )
                        }
                      />
                      <CellInput
                        value={relation.to}
                        placeholder={zh.schemaFill.relTo}
                        onChange={(v) =>
                          setRelations((prev) =>
                            prev.map((r, i) => (i === index ? { ...r, to: v } : r)),
                          )
                        }
                      />
                      <CellInput
                        value={relation.type}
                        placeholder={zh.schemaFill.relType}
                        onChange={(v) =>
                          setRelations((prev) =>
                            prev.map((r, i) => (i === index ? { ...r, type: v } : r)),
                          )
                        }
                      />
                      <CellInput
                        value={relation.where}
                        placeholder={zh.schemaFill.relWhere}
                        onChange={(v) =>
                          setRelations((prev) =>
                            prev.map((r, i) => (i === index ? { ...r, where: v } : r)),
                          )
                        }
                      />
                      <RemoveButton
                        onClick={() => setRelations((prev) => prev.filter((_, i) => i !== index))}
                      />
                    </div>
                  ))}
                </div>
              )}
              <AddButton
                label={zh.schemaFill.addRelation}
                onClick={() =>
                  setRelations((prev) => [...prev, { from: '', to: '', type: '', where: '' }])
                }
              />
            </section>
          </>
        )}

        {/* --- 外观设计：形态 / 视图面 / 省略视图 / 设计要点 --- */}
        {isDesign && (
          <>
            <section className="space-y-2">
              <p className="text-[13px] font-medium text-gray-700 dark:text-gray-200">
                {zh.schemaFill.productFormTitle}
              </p>
              <div className="grid sm:grid-cols-2 gap-2">
                {(['solid', 'planar'] as const).map((form) => {
                  const active = productForm === form
                  return (
                    <button
                      key={form}
                      type="button"
                      onClick={() => setProductForm(form)}
                      className={cn(
                        'text-left rounded-xl border-2 p-3 transition-colors',
                        active
                          ? 'border-[#6366f1] bg-indigo-50/50 dark:bg-indigo-500/10'
                          : 'border-gray-200 dark:border-gray-700 hover:border-indigo-300',
                      )}
                    >
                      <span className="block text-[13px] font-medium text-gray-900 dark:text-gray-100">
                        {zh.schemaFill.productForms[form]}
                      </span>
                      <span className="block mt-0.5 text-[11px] text-gray-500 dark:text-gray-400">
                        {zh.schemaFill.productFormHints[form]}
                      </span>
                    </button>
                  )
                })}
              </div>
            </section>

            <section className="space-y-2">
              <div>
                <p className="text-[13px] font-medium text-gray-700 dark:text-gray-200">
                  {zh.schemaFill.facesTitle}
                </p>
                <p className="text-[11px] text-gray-400 dark:text-gray-500">
                  {zh.schemaFill.facesHint}
                </p>
              </div>
              <div className="flex flex-wrap gap-1.5">
                {faceOptions.map((face) => {
                  const active = faces.includes(face)
                  return (
                    <button
                      key={face}
                      type="button"
                      onClick={() =>
                        setFaces((prev) =>
                          prev.includes(face) ? prev.filter((f) => f !== face) : [...prev, face],
                        )
                      }
                      className={cn(
                        'px-2.5 py-1 rounded-full text-[11px] border transition-colors',
                        active
                          ? 'border-[#6366f1] bg-indigo-50 dark:bg-indigo-500/10 text-[#6366f1] dark:text-indigo-400'
                          : 'border-gray-200 dark:border-gray-600 text-gray-500 dark:text-gray-400 hover:border-indigo-300',
                      )}
                    >
                      {face}
                    </button>
                  )
                })}
              </div>
            </section>

            <section className="space-y-2">
              <div>
                <p className="text-[13px] font-medium text-gray-700 dark:text-gray-200">
                  {zh.schemaFill.omittedTitle}
                </p>
                <p className="text-[11px] text-gray-400 dark:text-gray-500">
                  {zh.schemaFill.omittedHint}
                </p>
              </div>
              {omitted.length === 0 ? (
                <p className="text-[12px] text-gray-400 dark:text-gray-500">
                  {zh.schemaFill.emptyRows}
                </p>
              ) : (
                <div className="space-y-1.5">
                  {omitted.map((view, index) => (
                    <div
                      key={index}
                      className="grid grid-cols-1 sm:grid-cols-[1fr_2fr_28px] gap-2 items-center"
                    >
                      <CellInput
                        value={view.name}
                        placeholder={zh.schemaFill.omittedName}
                        onChange={(v) =>
                          setOmitted((prev) =>
                            prev.map((o, i) => (i === index ? { ...o, name: v } : o)),
                          )
                        }
                      />
                      <CellInput
                        value={view.reason}
                        placeholder={zh.schemaFill.omittedReason}
                        onChange={(v) =>
                          setOmitted((prev) =>
                            prev.map((o, i) => (i === index ? { ...o, reason: v } : o)),
                          )
                        }
                      />
                      <RemoveButton
                        onClick={() => setOmitted((prev) => prev.filter((_, i) => i !== index))}
                      />
                    </div>
                  ))}
                </div>
              )}
              <AddButton
                label={zh.schemaFill.addOmitted}
                onClick={() => setOmitted((prev) => [...prev, { name: '', reason: '' }])}
              />
            </section>

            <section className="space-y-2">
              <div>
                <p className="text-[13px] font-medium text-gray-700 dark:text-gray-200">
                  {zh.schemaFill.designPointsTitle}
                </p>
                <p className="text-[11px] text-gray-400 dark:text-gray-500">
                  {zh.schemaFill.designPointsHint}
                </p>
              </div>
              {designPoints.length === 0 ? (
                <p className="text-[12px] text-gray-400 dark:text-gray-500">
                  {zh.schemaFill.emptyRows}
                </p>
              ) : (
                <div className="space-y-1.5">
                  {designPoints.map((point, index) => (
                    <div key={index} className="flex items-center gap-2">
                      <CellInput
                        value={point}
                        onChange={(v) =>
                          setDesignPoints((prev) => prev.map((p, i) => (i === index ? v : p)))
                        }
                      />
                      <RemoveButton
                        onClick={() =>
                          setDesignPoints((prev) => prev.filter((_, i) => i !== index))
                        }
                      />
                    </div>
                  ))}
                </div>
              )}
              <AddButton
                label={zh.schemaFill.addDesignPoint}
                onClick={() => setDesignPoints((prev) => [...prev, ''])}
              />
            </section>
          </>
        )}

        {/* --- figure_plan 图片网格（两类型共用） --- */}
        <section className="space-y-2">
          <div className="flex items-baseline gap-2">
            <p className="text-[13px] font-medium text-gray-700 dark:text-gray-200">
              {zh.schemaFill.figuresTitle}
            </p>
            <span className="text-[11px] text-gray-400 dark:text-gray-500">
              {zh.schemaFill.usedCount(usedCount)}
            </span>
          </div>
          <p className="text-[11px] text-gray-400 dark:text-gray-500">
            {zh.schemaFill.figuresHint}
          </p>
          {figures.length === 0 ? (
            <p className="text-[12px] text-gray-400 dark:text-gray-500">
              {zh.schemaFill.figuresEmpty}
            </p>
          ) : (
            <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
              {figures.map((figure, index) => {
                const isCad = figure.kind === 'cad'
                return (
                  <div
                    key={figure.key}
                    className={cn(
                      'rounded-xl border p-2 transition-colors',
                      figure.use
                        ? 'border-[#6366f1] bg-indigo-50/30 dark:bg-indigo-500/5'
                        : 'border-gray-200 dark:border-gray-700',
                    )}
                  >
                    <div className="w-full aspect-[4/3] rounded-lg bg-gray-50 dark:bg-gray-900 overflow-hidden flex items-center justify-center">
                      {figure.url ? (
                        <img
                          src={figure.url}
                          alt={figure.path || `figure-${index + 1}`}
                          loading="lazy"
                          className="w-full h-full object-contain"
                        />
                      ) : (
                        <span className="px-2 text-[10px] text-gray-400 text-center break-all">
                          {figure.path || '—'}
                        </span>
                      )}
                    </div>
                    <div className="mt-1.5 flex flex-wrap items-center gap-1">
                      <Badge variant="indigo">
                        {zh.schemaFill.roles[figure.role] ?? figure.role}
                      </Badge>
                      <Badge variant={isCad ? 'amber' : 'neutral'}>
                        {zh.schemaFill.kinds[figure.kind] ?? figure.kind}
                      </Badge>
                      <span className="text-[10px] text-gray-400 dark:text-gray-500">
                        {zh.schemaFill.scoreLabel(Math.round(figure.score))}
                      </span>
                    </div>
                    <label
                      className={cn(
                        'mt-1.5 flex items-center gap-1.5 text-[11px]',
                        isCad
                          ? 'text-gray-400 dark:text-gray-500 cursor-not-allowed'
                          : 'text-gray-600 dark:text-gray-300 cursor-pointer',
                      )}
                    >
                      <input
                        type="checkbox"
                        checked={figure.use}
                        disabled={isCad}
                        onChange={(e) =>
                          setFigures((prev) =>
                            prev.map((f, i) => (i === index ? { ...f, use: e.target.checked } : f)),
                          )
                        }
                        className="w-3.5 h-3.5 accent-[#6366f1] disabled:opacity-50"
                      />
                      {isCad ? zh.schemaFill.cadNotUsed : zh.schemaFill.useInDoc}
                    </label>
                    {figure.reason && (
                      <p className="mt-1 text-[10px] leading-snug text-gray-400 dark:text-gray-500 line-clamp-2">
                        {str(figure.reason)}
                      </p>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </section>

        {/* --- 线稿绘制说明（缺合格线稿时） --- */}
        {briefNeeded && (
          <section className="rounded-xl border border-amber-200 dark:border-amber-500/30 bg-amber-50/60 dark:bg-amber-500/10 p-3 space-y-2">
            <div className="flex items-center gap-2">
              <p className="text-[13px] font-medium text-amber-700 dark:text-amber-400">
                {zh.schemaFill.briefTitle}
              </p>
              <button
                type="button"
                onClick={() => void copyBrief()}
                className="ml-auto inline-flex items-center gap-1 px-2 py-1 rounded-lg text-[11px] text-amber-700 dark:text-amber-400 hover:bg-amber-100/70 dark:hover:bg-amber-500/20 transition-colors"
              >
                <ClipboardDocumentIcon className="w-3.5 h-3.5" strokeWidth={1.5} />
                {zh.schemaFill.briefCopy}
              </button>
            </div>
            <p className="text-[11px] text-amber-700/90 dark:text-amber-400/90">
              {briefRisk || zh.schemaFill.briefHint}
            </p>
            {briefViews.length > 0 && (
              <p className="text-[11px] text-amber-700/80 dark:text-amber-400/80">
                {zh.schemaFill.briefViews}：{briefViews.map((v) => v.name).join('、')}
              </p>
            )}
            <textarea
              readOnly
              rows={6}
              aria-label={zh.schemaFill.briefTitle}
              value={briefText(briefViews, briefForbid, briefNote)}
              onFocus={(e) => e.currentTarget.select()}
              className="w-full rounded-lg border border-amber-200 dark:border-amber-500/30 bg-white/80 dark:bg-gray-900/60 px-3 py-2 text-[12px] leading-relaxed text-gray-700 dark:text-gray-200 font-mono outline-none resize-y"
            />
          </section>
        )}
      </div>
    </StageCardShell>
  )
}
