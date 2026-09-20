import { api } from '../client'
import type { AssistantAnswer } from '../../domain/ask'
import type { ConfirmOut, AiRecordOut } from '../types'

export function askQuestion(question: string, scenarioKind: string) {
  return api.post<AssistantAnswer>('/assistant/ask', { question, scenario_kind: scenarioKind })
}

export function confirmProposal(aiRecordId: number, confirmedBy: string) {
  return api.post<ConfirmOut>(`/ai/records/${aiRecordId}/confirm`, { confirmed_by: confirmedBy })
}

export function rejectProposal(aiRecordId: number, rejectedBy: string) {
  return api.post<AiRecordOut>(`/ai/records/${aiRecordId}/reject`, { rejected_by: rejectedBy })
}
