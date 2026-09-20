/**
 * The plain-language layer — ported from nvplan/api/static/js/terms.js. Same rule as the
 * vanilla UI (UI.md: "the interface must not speak in our shorthand"): plain language is the
 * PRIMARY label; the internal identifier (a code, an enum value, a symbol) is never removed —
 * it renders secondary, alongside the plain word rather than in place of it.
 *
 * One wording per identifier, shared by every screen. No arithmetic, no fetching.
 */

export const CATEGORY_NAMES: Record<string, string> = {
  REV: 'Revenue',
  MAT: 'Material costs',
  EXT: 'External services',
  PERS: 'Personnel costs',
  OTH: 'Other costs',
  DEPR: 'Depreciation',
}
export function categoryName(code: string | null | undefined): string {
  if (!code) return '-'
  return CATEGORY_NAMES[code] || code
}

export interface Term {
  label: string
  hint?: string
  symbol?: string
}

export const PATH_TERMS: Record<string, Term> = {
  cascaded: { label: 'follows revenue', hint: 'cascaded — moves automatically when revenue moves' },
  valorized: {
    label: 'grown on its own trend',
    hint: 'valorized — grown on its own historical trend, independent of revenue',
  },
  decided: { label: 'set by a decision', hint: 'decided — a decision in the brain set this figure directly' },
  ai_proposed: {
    label: 'proposed by the assistant, confirmed by a person',
    hint: 'ai_proposed — an AI proposal a named person confirmed',
  },
}
export function pathTerm(path: string | null | undefined): Term {
  if (!path) return { label: '-' }
  return PATH_TERMS[path] || { label: path, hint: path }
}
/** Marked paths (UI.md: "A figure whose path is decided or ai_proposed is visually marked"). */
export const MARKED_PATHS = new Set(['decided', 'ai_proposed'])

export const PARAM_TERMS: Record<string, Term> = {
  alpha: { label: 'fixed component', symbol: 'alpha', hint: 'the part of the cost that does not move with revenue' },
  beta: { label: 'variable rate per unit of revenue', symbol: 'beta', hint: 'how much this moves per unit of revenue' },
  r_squared: {
    label: 'fit quality',
    symbol: 'R²',
    hint: 'how well the fitted line matches the historical actuals — 0 to 1, higher is better',
  },
  valorization_rate: {
    label: 'growth of the fixed component',
    symbol: 'v',
    hint: "the fixed component's own year-on-year growth rate",
  },
  v: { label: 'growth of the fixed component', symbol: 'v', hint: "the fixed component's own year-on-year growth rate" },
  growth_rate: {
    label: 'growth rate of the default path',
    symbol: 'g',
    hint: 'the growth rate used for the valorized default path',
  },
  g: { label: 'growth rate of the default path', symbol: 'g', hint: 'the growth rate used for the valorized default path' },
  window_from: { label: 'fit window start', symbol: 'window_from' },
  window_to: { label: 'fit window end', symbol: 'window_to' },
  calc_version: { label: 'calculation version', symbol: 'calc_version' },
  spread: { label: 'spread', symbol: 'spread' },
  t0: { label: 'base year', symbol: 't0' },
  n: { label: 'sample count', symbol: 'n' },
}
export function paramTerm(key: string): Term | null {
  return PARAM_TERMS[key] || null
}

export const TAG_TERMS: Record<string, Term> = {
  ingestion: { label: 'synthesized record', hint: 'ingestion — a record we wrote from raw material' },
  source: { label: 'raw source document', hint: 'source — an unedited artifact: a transcript or a document' },
  stakeholder_verbal: { label: 'said by a stakeholder', hint: 'stakeholder-verbal' },
  intuition: { label: 'instinct call', hint: 'intuition — a judgment call, no external evidence' },
  industry_knowledge: { label: 'general industry knowledge', hint: 'industry-knowledge — no specific source' },
  chat: { label: 'said in chat, no record kept', hint: 'chat, no artifact' },
  computed: { label: 'calculated by the engine', hint: 'computed — resolves to a real calculation in the engine' },
}
export function tagTerm(tagRaw: string | null | undefined): Term {
  if (!tagRaw) return { label: 'untagged', hint: tagRaw ?? '' }
  const s = String(tagRaw).toLowerCase()
  if (s.startsWith('[ingestion') || s.startsWith('ingestion')) return TAG_TERMS.ingestion
  if (s.startsWith('[source') || s.startsWith('source')) return TAG_TERMS.source
  if (s.includes('stakeholder-verbal')) return TAG_TERMS.stakeholder_verbal
  if (s.includes('intuition')) return TAG_TERMS.intuition
  if (s.includes('industry-knowledge')) return TAG_TERMS.industry_knowledge
  if (s.includes('chat')) return TAG_TERMS.chat
  if (s.includes('computed')) return TAG_TERMS.computed
  return { label: tagRaw, hint: tagRaw }
}

export const CLAIM_KIND_TERMS: Record<string, string> = {
  decision: 'decision',
  hypothesis: 'hypothesis',
  ingestion: 'research note',
  knowledge: 'product knowledge',
  computed: 'calculated figure',
  ai_proposal: 'AI proposal, confirmed by a person',
}
export function claimKindLabel(kind: string | null | undefined): string {
  if (!kind) return '-'
  return CLAIM_KIND_TERMS[kind] || kind
}

export const EVIDENCE_SECTION_TERMS: Record<string, string> = {
  evidence_for: 'supporting',
  evidence_against: 'against',
  not_doing: 'explicitly not doing',
}
export function evidenceSectionLabel(section: string): string {
  return EVIDENCE_SECTION_TERMS[section] || section
}

export const STATUS_TERMS: Record<string, string> = {
  pending: 'not yet decided',
  decided: 'decided',
  superseded: 'superseded by a newer decision',
  open: 'still open',
  supported: 'supported by evidence',
  refuted: 'refuted',
  proposed: 'awaiting confirmation',
  confirmed: 'confirmed',
  rejected: 'rejected',
  within: 'within threshold',
  marginal: 'marginal',
  missed: 'missed threshold',
}
export function statusLabel(status: string | null | undefined): string {
  if (!status) return '-'
  return STATUS_TERMS[status] || status
}

export const TOUCHPOINT_TERMS: Record<string, string> = {
  env_scan: 'Environment scan',
  revenue_proposal: 'Revenue proposal',
  deviation_explanation: 'Deviation explanation',
  assistant: 'Assistant conversation',
}
export function touchpointLabel(tp: string): string {
  return TOUCHPOINT_TERMS[tp] || tp
}

export const FIGURE_REF_KIND_TERMS: Record<string, string> = {
  plan_value: 'planned figure',
  parameter: 'fitted parameter',
  derivation: 'how this was calculated',
  claim: 'decision',
}
export function figureRefKindLabel(kind: string): string {
  return FIGURE_REF_KIND_TERMS[kind] || kind
}

// The assistant writes its own figure labels in the engine's shorthand (e.g. "REV 2026 (base,
// valorized)"); word-boundary substitution replaces the identifiers it used with the same plain
// words the rest of the app uses, keeping the model's exact sentence structure.
const CODE_RE = new RegExp(`\\b(${Object.keys(CATEGORY_NAMES).join('|')})\\b`, 'g')
const PATH_RE = new RegExp(`\\b(${Object.keys(PATH_TERMS).join('|')})\\b`, 'g')

export function prettifyLabel(text: string | null | undefined): string {
  if (!text) return text ?? ''
  return String(text)
    .replace(CODE_RE, (m) => categoryName(m))
    .replace(PATH_RE, (m) => pathTerm(m).label)
}
