import { useMemo, useState } from 'react'
import { ClipboardDocumentListIcon } from '@heroicons/react/24/outline'
import { zh } from '../../i18n/zh'
import { Input } from '../ui/Input'
import { SegmentedToggle, type SegmentedOption } from '../ui/SegmentedToggle'
import { StageCardShell, type StageCardProps } from '../pipeline/StageCardShell'
import type { InteractionRequiredEvent } from '../../types/stream'
import { asRecord, pickBool, pickStr, readDefault, readSchema, str } from './stagePayload'

/** 专利类型选项值（intake.md Q2：暂不确定 = 仍按发明推进）。 */
export type IntakeTypeValue = 'invention' | 'utility_model' | 'design' | 'unsure'

const TYPE_VALUES: readonly IntakeTypeValue[] = ['invention', 'utility_model', 'design', 'unsure']

/** schema.properties.patent_type.enum 覆盖时按后端枚举渲染，否则四段固定。 */
function readTypeOptions(payload: InteractionRequiredEvent | null): SegmentedOption<string>[] {
  const schema = readSchema(payload)
  const props = asRecord(schema.properties)
  const prop = asRecord(props?.patent_type) ?? asRecord(props?.type)
  const raw = Array.isArray(prop?.enum) ? (prop.enum as unknown[]) : null
  const values =
    raw && raw.length > 0 ? raw.map((v) => str(v)).filter(Boolean) : [...TYPE_VALUES]
  return values.map((value) => ({ value, label: zh.stages.intake.types[value] ?? value }))
}

interface ContactValue {
  name: string
  phone: string
  email: string
}

function readContact(source: Record<string, unknown>): ContactValue {
  const contact = asRecord(source.contact) ?? source
  return {
    name: pickStr(contact, 'name', 'contact_name'),
    phone: pickStr(contact, 'phone', 'tel', 'contact_phone'),
    email: pickStr(contact, 'email', 'mail', 'contact_email'),
  }
}

/**
 * §2.8 模块 A / A1 边界录入卡（intake.md 三问）：
 * ①技术主题一句话 ②专利类型四段 SegmentedToggle（默认发明，附「方法/系统 ≠ 专利类型」提示）
 * ③文头联系人三输入框 + 「使用占位符」勾选。提交
 * `{topic, patent_type, contact:{name,phone,email}, contact_placeholder}`。
 */
export function IntakeCard({ stage, submit, skip, busy }: StageCardProps) {
  const payload = stage.payload as InteractionRequiredEvent | null
  const defaults = useMemo(() => {
    const result = asRecord(stage.result)
    return result ?? readDefault(payload)
  }, [payload, stage.result])

  const typeOptions = useMemo(() => readTypeOptions(payload), [payload])

  const [topic, setTopic] = useState(() =>
    pickStr(defaults, 'topic', 'subject', 'tech_topic', 'module'),
  )
  const [patentType, setPatentType] = useState<string>(() => {
    const raw = pickStr(defaults, 'patent_type', 'type')
    return raw && typeOptions.some((o) => o.value === raw) ? raw : 'invention'
  })
  const [usePlaceholder, setUsePlaceholder] = useState(() =>
    pickBool(defaults, ['contact_placeholder', 'use_placeholder', 'placeholder'], false),
  )
  const [contact, setContact] = useState<ContactValue>(() => readContact(defaults))

  const typeLabel = zh.stages.intake.types[patentType] ?? patentType
  const isDefaultType = patentType === 'invention' || patentType === 'unsure'

  const handleSubmit = () => {
    const filled: ContactValue = usePlaceholder
      ? {
          name: zh.stages.intake.placeholderText,
          phone: zh.stages.intake.placeholderText,
          email: zh.stages.intake.placeholderText,
        }
      : contact
    submit({
      topic: topic.trim(),
      // 「暂不确定」按 intake.md 硬性默认规则回落为发明
      patent_type: patentType === 'unsure' ? 'invention' : patentType,
      patent_type_choice: patentType,
      contact_placeholder: usePlaceholder,
      contact: filled,
    })
  }

  const contactField = (
    label: string,
    value: string,
    onChange: (next: string) => void,
    type = 'text',
  ) => (
    <label className="block">
      <span className="block text-[11px] text-gray-400 dark:text-gray-500 mb-1">{label}</span>
      <Input
        type={type}
        value={usePlaceholder ? zh.stages.intake.placeholderText : value}
        disabled={usePlaceholder}
        placeholder={label}
        onChange={(e) => onChange(e.target.value)}
      />
    </label>
  )

  return (
    <StageCardShell
      stage={stage}
      icon={ClipboardDocumentListIcon}
      title={zh.stage.kinds.intake}
      summary={zh.stages.intake.summary(typeLabel, topic.trim())}
      onConfirm={handleSubmit}
      onSkip={skip}
      confirmLabel={zh.stages.intake.confirm}
      busy={busy}
    >
      <div className="space-y-4">
        <p className="text-sm text-gray-700 dark:text-gray-300">
          {payload?.prompt || zh.stages.intake.lead}
        </p>

        {/* Q1 技术主题 */}
        <div>
          <label className="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-1.5">
            {zh.stages.intake.topicLabel}
          </label>
          <Input
            value={topic}
            placeholder={zh.stages.intake.topicPlaceholder}
            onChange={(e) => setTopic(e.target.value)}
          />
        </div>

        {/* Q2 专利类型 */}
        <div>
          <label className="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-1.5">
            {zh.stages.intake.typeLabel}
            {isDefaultType && (
              <span className="ml-1 font-normal text-gray-400 dark:text-gray-500">
                {zh.stages.intake.typeDefaultSuffix}
              </span>
            )}
          </label>
          <SegmentedToggle
            options={typeOptions}
            value={patentType}
            onChange={setPatentType}
            className="w-full sm:w-auto sm:inline-flex"
          />
          <p className="mt-1.5 text-[11px] leading-relaxed text-gray-400 dark:text-gray-500">
            {zh.stages.intake.typeHint}
          </p>
        </div>

        {/* Q3 联系人 */}
        <div>
          <div className="flex items-center justify-between gap-3 mb-1.5">
            <label className="text-xs font-medium text-gray-500 dark:text-gray-400">
              {zh.stages.intake.contactLabel}
            </label>
            <label className="inline-flex items-center gap-1.5 text-[11px] text-gray-500 dark:text-gray-400 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={usePlaceholder}
                onChange={(e) => setUsePlaceholder(e.target.checked)}
                className="w-3.5 h-3.5 rounded border-gray-300 dark:border-gray-600 accent-[#6366f1]"
              />
              {zh.stages.intake.contactPlaceholderToggle}
            </label>
          </div>
          <div className="grid sm:grid-cols-3 gap-2">
            {contactField(zh.stages.intake.contactName, contact.name, (name) =>
              setContact((prev) => ({ ...prev, name })),
            )}
            {contactField(zh.stages.intake.contactPhone, contact.phone, (phone) =>
              setContact((prev) => ({ ...prev, phone })),
            )}
            {contactField(
              zh.stages.intake.contactEmail,
              contact.email,
              (email) => setContact((prev) => ({ ...prev, email })),
              'email',
            )}
          </div>
        </div>
      </div>
    </StageCardShell>
  )
}
