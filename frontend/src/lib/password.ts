/**
 * 密码强度与合法性校验（与 backend/app/models/auth.py 的规则保持一致）：
 * ≥8 位、且不能是纯数字或纯字母。前端实时提示只是体验，后端仍会独立校验。
 */

export const MIN_PASSWORD_LEN = 8

export type PasswordStrength = 'weak' | 'medium' | 'strong'

export interface PasswordCheck {
  /** ≥ 8 位。 */
  length: boolean
  /** 不是纯数字、也不是纯字母。 */
  mixed: boolean
  /** 满足后端最低要求（length && mixed）。 */
  valid: boolean
  strength: PasswordStrength
  /** 0–3，用于强度条的段数。 */
  score: number
}

const ALL_DIGITS = /^\d+$/
const ALL_LETTERS = /^[A-Za-z]+$/
const HAS_LOWER = /[a-z]/
const HAS_UPPER = /[A-Z]/
const HAS_DIGIT = /\d/
const HAS_SYMBOL = /[^A-Za-z0-9]/

export function checkPassword(password: string): PasswordCheck {
  const length = password.length >= MIN_PASSWORD_LEN
  // 空串在 isdigit()/isalpha() 下皆为 false，与后端一致地视为「未通过长度」而非混合失败
  const mixed = password.length > 0 && !ALL_DIGITS.test(password) && !ALL_LETTERS.test(password)
  const valid = length && mixed

  let variety = 0
  if (HAS_LOWER.test(password)) variety += 1
  if (HAS_UPPER.test(password)) variety += 1
  if (HAS_DIGIT.test(password)) variety += 1
  if (HAS_SYMBOL.test(password)) variety += 1

  let score = 0
  if (valid) {
    score = 1
    if (variety >= 3 || password.length >= 12) score = 2
    if (variety >= 3 && password.length >= 12) score = 3
  }
  const strength: PasswordStrength = score >= 3 ? 'strong' : score === 2 ? 'medium' : 'weak'

  return { length, mixed, valid, strength, score }
}
