/**
 * The lineage tree shape returned by `GET /trace/plan-value/{id}` and
 * `GET /trace/statement-line/{id}` — `nvplan/services/trace.py`'s `TraceNode.to_dict()`.
 *
 * Both routes are declared `response_model=None` in nvplan/api/app.py (the tree is recursive and
 * shaped like a small tagged union per node — the reindexed generator would need real Pydantic
 * models to describe that, and adding them is outside this build's ownership of
 * nvplan/api/schemas.py). So, unlike every other screen in this app, this type is hand-written
 * here rather than imported from `../api/schema`, matching field-for-field what
 * nvplan/api/static/js/views/trace.js already reads off the live response. Flagged in the final
 * report as a gap worth giving trace/ask real response models for, so this file can be deleted.
 */

export interface TraceAiBlock {
  ai_record_id: number
  touchpoint: string
  status: string
  model_version: string
  proposed_value: number | null
  year: number | null
  rationale: string | null
  confirmed_by: string | null
  confirmed_at: string | null
  created_at: string | null
  prompt_text: string | null
  response_text: string | null
}

export interface TraceEvidence {
  tag_raw: string
  text: string
}

export interface TraceClaim {
  claim_id: number
  slug: string
  title: string | null
  status: string | null
  decided_on: string | null
  reversal_condition: string | null
  evidence: TraceEvidence[]
}

export interface TraceNode {
  kind: string
  label: string
  value: number | null
  path: string | null
  ref: boolean
  truncated?: boolean
  source_label: string | null
  formula_text: string | null
  parameters: Record<string, unknown>
  inputs: Record<string, unknown> & { points?: Record<string, unknown>[] }
  ai: TraceAiBlock | null
  claim: TraceClaim | null
  children: TraceNode[]
  // Server-side bookkeeping (nvplan/services/trace.py TraceNode.to_dict) this UI does not
  // render directly, kept so the type does not silently drop fields a future view might want.
  key?: string | null
  derivation_id?: number | null
  ref_id?: number | null
  meta?: Record<string, unknown>
}
