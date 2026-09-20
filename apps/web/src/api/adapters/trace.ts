import { api, getText, type ApiResult } from '../client'
import type { TraceNode } from '../../domain/trace'

export type TraceKind = 'plan-value' | 'statement-line'

export function traceEndpoint(kind: TraceKind, id: number | string): string {
  return kind === 'statement-line' ? `/trace/statement-line/${id}` : `/trace/plan-value/${id}`
}

/** `../../domain/trace.ts` documents why this is a hand asserted type rather than a generated
 * one: both trace routes are `response_model=None` on the server. */
export function fetchTrace(kind: TraceKind, id: number | string): Promise<ApiResult<TraceNode>> {
  return api.get<TraceNode>(traceEndpoint(kind, id))
}

export function fetchTraceText(kind: TraceKind, id: number | string) {
  return getText(`${traceEndpoint(kind, id)}?format=text`)
}
