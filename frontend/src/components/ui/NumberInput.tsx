import { useEffect, useState } from 'react'
import { Input } from './Input'
import type { InputProps } from './Input'

export interface NumberInputProps extends Omit<InputProps, 'value' | 'onChange' | 'type'> {
  value: number
  onChange: (value: number) => void
  min?: number
  max?: number
  /** 清空并失焦时回落的值（默认取 min，再默认 0）。 */
  fallback?: number
}

/**
 * 受控数字输入框。
 *
 * 直接把 `value={number}` 绑到 <input type="number"> 会有个恼人的后果：
 * 用户全选删除时 onChange 拿到的是空串，`Number('')` 得 0（或回落旧值），
 * 于是输入框立刻被写回一个数字 —— 用户既删不掉它，接着输入 128 只会得到
 * "0128"。这里在编辑期间用本地字符串承接原始输入，允许中途为空，失焦时才
 * 归一化并钳制到 [min, max]，编辑过程与最终值两不耽误。
 */
export function NumberInput({
  value,
  onChange,
  min,
  max,
  fallback,
  onFocus,
  onBlur,
  ...rest
}: NumberInputProps) {
  const [raw, setRaw] = useState(() => String(value))
  const [editing, setEditing] = useState(false)

  // 非编辑态时跟随外部值（例如「重置」「读取配置」后回填）
  useEffect(() => {
    if (!editing) setRaw(String(value))
  }, [value, editing])

  const clamp = (n: number) => {
    let v = n
    if (typeof min === 'number' && v < min) v = min
    if (typeof max === 'number' && v > max) v = max
    return v
  }

  return (
    <Input
      {...rest}
      type="number"
      min={min}
      max={max}
      value={editing ? raw : String(value)}
      onFocus={(e) => {
        setEditing(true)
        setRaw(String(value))
        onFocus?.(e)
      }}
      onChange={(e) => {
        const next = e.target.value
        setRaw(next)
        // 编辑中途允许空串与 "-" 这类不完整输入，此时不回写父状态
        if (next === '') return
        const n = Number(next)
        if (Number.isFinite(n)) onChange(n)
      }}
      onBlur={(e) => {
        setEditing(false)
        const n = Number(raw)
        const base = fallback ?? min ?? 0
        const final = raw === '' || !Number.isFinite(n) ? base : clamp(n)
        setRaw(String(final))
        if (final !== value) onChange(final)
        onBlur?.(e)
      }}
    />
  )
}
