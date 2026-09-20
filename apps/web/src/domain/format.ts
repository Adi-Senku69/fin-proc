/**
 * Display formatting only — ported from nvplan/api/static/js/format.js. Nothing here computes a
 * new figure; each function takes a value the API already returned and changes how it is
 * written on screen (thousands separators, decimal places, percent sign, local date string).
 * Same rule as the vanilla UI (UI.md): "No arithmetic in JavaScript/TypeScript beyond
 * formatting."
 */

export function fmtNum(v: number | string | null | undefined, decimals?: number): string {
  if (v === null || v === undefined || v === '') return '-'
  if (typeof v !== 'number') return String(v)
  if (!Number.isFinite(v)) return String(v)
  const d = decimals ?? (Math.abs(v) >= 100 ? 1 : 4)
  return v.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d })
}

export function fmtMoney(v: number | null | undefined, unit = 'k EUR'): string {
  if (v === null || v === undefined) return '-'
  if (typeof v !== 'number' || !Number.isFinite(v)) return String(v)
  return `${v.toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 })} ${unit}`
}

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return '-'
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return String(iso)
    return d.toLocaleString()
  } catch {
    return String(iso)
  }
}

export function fmtCell(v: unknown): string {
  if (v === null || v === undefined) return '-'
  if (typeof v === 'number') return fmtNum(v)
  if (typeof v === 'boolean') return v ? 'true' : 'false'
  if (Array.isArray(v) || typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

/** Keys that name a calendar year, never a quantity — see format.js's YEAR_PARAM_KEYS. */
const YEAR_PARAM_KEYS = new Set([
  't',
  't0',
  'year',
  'window_from',
  'window_to',
  'prev_year',
  'target_year',
  'train_from',
  'train_to',
])

export function fmtParam(key: string, v: unknown): string {
  if (typeof v === 'number' && Number.isFinite(v)) {
    if (YEAR_PARAM_KEYS.has(key)) {
      return v.toLocaleString(undefined, { useGrouping: false, minimumFractionDigits: 0, maximumFractionDigits: 0 })
    }
    if (Number.isInteger(v)) {
      return v.toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 0 })
    }
  }
  return fmtCell(v)
}
