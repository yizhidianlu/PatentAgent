import { useId, useMemo, useState } from 'react'
import { PencilSquareIcon } from '@heroicons/react/24/outline'
import { zh } from '../../i18n/zh'
import { Input } from '../ui/Input'
import { Select } from '../ui/Select'
import { ToggleSwitch } from '../ui/ToggleSwitch'
import { StageCardShell, type StageCardProps } from '../pipeline/StageCardShell'
import type { InteractionRequiredEvent } from '../../types/stream'
import { IntakeCard } from './IntakeCard'
import { asRecord, pickStr } from './stagePayload'

/** JSON Schema 单属性（宽松镜像）。 */
interface SchemaProp {
  type?: string
  title?: string
  description?: string
  enum?: unknown[]
  format?: string
  maxLength?: number
  items?: { type?: string; enum?: unknown[] }
  default?: unknown
}

interface ObjectSchema {
  type?: string
  title?: string
  description?: string
  properties?: Record<string, SchemaProp>
  required?: string[]
}

type FormValues = Record<string, unknown>

function textareaLike(prop: SchemaProp): boolean {
  return prop.format === 'textarea' || (prop.maxLength ?? 0) > 120
}

/** 初始值：payload.default > prop.default > 类型零值。 */
function initValues(schema: ObjectSchema, defaults: unknown): FormValues {
  const base = (defaults && typeof defaults === 'object' ? defaults : {}) as FormValues
  const values: FormValues = {}
  for (const [key, prop] of Object.entries(schema.properties ?? {})) {
    if (key in base) values[key] = base[key]
    else if (prop.default !== undefined) values[key] = prop.default
    else if (prop.type === 'boolean') values[key] = false
    else if (prop.type === 'array') values[key] = []
    else values[key] = ''
  }
  return values
}

const textareaCls =
  'w-full rounded-xl border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 ' +
  'px-3.5 py-2.5 text-sm text-gray-900 dark:text-gray-100 placeholder:text-gray-400 ' +
  'outline-none transition-colors focus:border-[#6366f1] focus:ring-4 focus:ring-indigo-500/10 ' +
  'min-h-[96px] scrollbar-thin'

/**
 * 交底书 A1 边界录入的兼容判别：后端若以 kind='form' 下发 intake 表单，
 * 依 schema.hint（或 schema 形状：patent_type + contact 字段 / step_key=intake）
 * 改派专用 IntakeCard 渲染（两种 kind 都要兼容）。
 */
function isIntakeForm(payload: InteractionRequiredEvent | null): boolean {
  const schema = asRecord(payload?.schema)
  const hint = pickStr(schema, 'hint', 'x-hint', 'ui', 'form', 'card', 'widget').toLowerCase()
  if (hint.includes('intake')) return true
  const props = asRecord(schema?.properties)
  if (!props) return false
  const hasType = 'patent_type' in props
  const hasContact = 'contact' in props || 'contact_placeholder' in props
  return hasType && (hasContact || payload?.step_key === 'intake')
}

/**
 * §4 通用 JSON schema 驱动表单卡：string / enum / boolean / textarea / string[]
 * 字段渲染（p2p input_check gaps 确认等）。提交 = 收集后的对象。
 * 交底书 intake 表单（kind='form' + hint）改派 IntakeCard。
 */
export function FormCard(props: StageCardProps) {
  if (isIntakeForm(props.stage.payload as InteractionRequiredEvent | null)) {
    return <IntakeCard {...props} />
  }
  return <SchemaFormCard {...props} />
}

function SchemaFormCard({ stage, submit, skip, busy }: StageCardProps) {
  const payload = stage.payload as InteractionRequiredEvent | null
  const schema = (payload?.schema ?? {}) as ObjectSchema
  const props = schema.properties ?? {}

  const initial = useMemo(
    () => initValues(schema, (stage.result as FormValues | undefined) ?? payload?.default),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [stage.id],
  )
  const [values, setValues] = useState<FormValues>(initial)
  const formId = useId()

  const setValue = (key: string, value: unknown) =>
    setValues((prev) => ({ ...prev, [key]: value }))

  const handleSubmit = () => {
    const out: FormValues = {}
    for (const [key, prop] of Object.entries(props)) {
      const v = values[key]
      if (prop.type === 'array') {
        out[key] = Array.isArray(v)
          ? v
          : String(v ?? '')
              .split('\n')
              .map((line) => line.trim())
              .filter(Boolean)
      } else if (prop.type === 'boolean') {
        out[key] = Boolean(v)
      } else {
        out[key] = v ?? ''
      }
    }
    submit(out)
  }

  const renderField = (key: string, prop: SchemaProp) => {
    const label = prop.title ?? key
    const value = values[key]

    if (prop.type === 'boolean') {
      return (
        <div key={key} className="flex items-center justify-between gap-3">
          <div className="min-w-0">
            <p className="text-sm text-gray-800 dark:text-gray-200">{label}</p>
            {prop.description && (
              <p className="text-xs text-gray-400 dark:text-gray-500">{prop.description}</p>
            )}
          </div>
          <ToggleSwitch
            checked={Boolean(value)}
            onChange={(checked) => setValue(key, checked)}
            label={label}
          />
        </div>
      )
    }

    // label 与控件用 htmlFor/id 显式关联（读屏与点击 label 聚焦都依赖它）
    const fieldId = `${formId}-${key}`
    const labelEl = (
      <label
        htmlFor={fieldId}
        className="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-1.5"
      >
        {label}
        {prop.description && (
          <span className="ml-2 font-normal text-gray-400 dark:text-gray-500">
            {prop.description}
          </span>
        )}
      </label>
    )

    if (Array.isArray(prop.enum) && prop.enum.length > 0) {
      return (
        <div key={key}>
          {labelEl}
          <Select
            id={fieldId}
            value={String(value ?? '')}
            onChange={(e) => setValue(key, e.target.value)}
          >
            <option value="" disabled>
              {zh.common.empty}
            </option>
            {prop.enum.map((opt) => (
              <option key={String(opt)} value={String(opt)}>
                {String(opt)}
              </option>
            ))}
          </Select>
        </div>
      )
    }

    if (prop.type === 'array') {
      const text = Array.isArray(value) ? (value as unknown[]).join('\n') : String(value ?? '')
      return (
        <div key={key}>
          {labelEl}
          <textarea
            id={fieldId}
            className={textareaCls}
            value={text}
            placeholder={zh.stages.form.arrayHint}
            onChange={(e) => setValue(key, e.target.value)}
          />
        </div>
      )
    }

    if (textareaLike(prop)) {
      return (
        <div key={key}>
          {labelEl}
          <textarea
            id={fieldId}
            className={textareaCls}
            value={String(value ?? '')}
            onChange={(e) => setValue(key, e.target.value)}
          />
        </div>
      )
    }

    return (
      <div key={key}>
        {labelEl}
        <Input
          id={fieldId}
          value={String(value ?? '')}
          onChange={(e) => setValue(key, e.target.value)}
        />
      </div>
    )
  }

  return (
    <StageCardShell
      stage={stage}
      icon={PencilSquareIcon}
      summary={zh.stages.form.summary}
      onConfirm={handleSubmit}
      onSkip={skip}
      confirmLabel={zh.stages.form.confirm}
      busy={busy}
    >
      <div className="space-y-4">
        {payload?.prompt && (
          <p className="text-sm text-gray-700 dark:text-gray-300">{payload.prompt}</p>
        )}
        {Object.entries(props).map(([key, prop]) => renderField(key, prop))}
      </div>
    </StageCardShell>
  )
}
