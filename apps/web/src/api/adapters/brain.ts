import { api } from '../client'
import type { BrainValidateOut, ClaimDetailOut, ClaimOut, ImpactRowOut } from '../types'

export function fetchValidation() {
  return api.get<BrainValidateOut>('/brain/validate')
}

export function fetchClaims() {
  return api.get<ClaimOut[]>('/brain/claims')
}

export function fetchClaimDetail(claimId: string | number) {
  return api.get<ClaimDetailOut>(`/brain/claims/${claimId}`)
}

export function fetchClaimImpact(claimId: string | number) {
  return api.get<ImpactRowOut[]>(`/brain/claims/${claimId}/impact`)
}
