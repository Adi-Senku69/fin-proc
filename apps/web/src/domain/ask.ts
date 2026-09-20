/**
 * `POST /assistant/ask`'s response shape — `nvplan.ai.assistant.AssistantAnswer` (and its
 * `Segment`/`Proposal` members). The route is declared `response_model=None` in
 * nvplan/api/app.py, so — same situation as `../domain/trace.ts` — this is hand-written rather
 * than generated, matching `AssistantAnswer` field-for-field. A 422 from this endpoint is not
 * this shape at all: it is a plain FastAPI `{ detail: string }` refusal, handled by
 * `ApiErr.message` in `../api/client.ts` before this type is ever reached.
 */

export interface FigureRef {
  kind: 'plan_value' | 'parameter' | 'derivation' | 'claim'
  id: number
}

export interface TextSegment {
  type: 'text'
  text: string
}

export interface FigureSegment {
  type: 'figure'
  label: string
  value: number
  unit: string
  ref: FigureRef
}

export interface ClaimSegment {
  type: 'claim'
  claim_id: number
  title: string
  status: string
}

export type AskSegment = TextSegment | FigureSegment | ClaimSegment

export interface AskProposal {
  ai_record_id: number | null
  year: number
  proposed_value: number
  rationale: string
  category_code: string
}

export interface AssistantAnswer {
  segments: AskSegment[]
  proposal: AskProposal | null
  ai_record_id: number
  usage: Record<string, number>
}
