/**
 * 把 token 数量写成人眼好认的形式：1048576 → `1M`，65536 → `64K`。
 *
 * 难点在于进制不统一：厂商宣传的「128K 上下文」有的是 128000（十进制），
 * 有的是 131072（2^17）。硬用其中一种换算，另一种就会显示成 125K 或 131.1K，
 * 与厂商说法对不上，反而更让人犯嘀咕。
 *
 * 所以两种都试，取能得到「整齐」结果的那个（整数或半整数），十进制优先——
 * 它更贴近厂商的说法。两种都不整齐时退回二进制并保留一位小数。
 */

/** 整数或 .5 结尾都算整齐；1.25M 这种就不算。 */
function isTidy(v: number): boolean {
  return Number.isInteger(v) || Number.isInteger(v * 2)
}

/** 去掉多余的 `.0`：1.0 → `1`，1.5 → `1.5`。 */
function trim(v: number): string {
  return Number.isInteger(v) ? String(v) : v.toFixed(1).replace(/\.0$/, '')
}

export function formatTokens(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return ''
  // 不足 1000 时简写就是原数字本身，显示出来只是重复一遍，不如不显示。
  // 返回空串 = 「没有更好认的写法」，调用方据此决定要不要渲染。
  if (n < 1000) return ''

  const units: Array<{ unit: string; dec: number; bin: number }> = [
    { unit: 'M', dec: 1e6, bin: 1024 ** 2 },
    { unit: 'K', dec: 1e3, bin: 1024 },
  ]

  for (const { unit, dec, bin } of units) {
    // 小于该量级就跳过，免得出现 0.06M 这种读着更费劲的写法
    if (n < Math.min(dec, bin)) continue
    for (const div of [dec, bin]) {
      const v = n / div
      if (v >= 1 && isTidy(v)) return trim(v) + unit
    }
  }

  // 都不整齐：按二进制取一位小数，至少给个量级感（如 190000 → 185.5K）
  for (const { unit, bin } of units) {
    if (n >= bin) return trim(Math.round((n / bin) * 10) / 10) + unit
  }
  return String(n)
}
