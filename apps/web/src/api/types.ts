/**
 * Named aliases onto the generated schema (`./schema.d.ts`). Nothing here hand-writes a shape
 * the server owns — a change to a `nvplan/api/schemas.py` model becomes a TypeScript compile
 * error at the adapter that reads it, rather than a runtime surprise in a component.
 */
import type { components } from './schema'

export type HealthOut = components['schemas']['HealthOut']

export type PlanGridOut = components['schemas']['PlanGridOut']
export type GridRow = components['schemas']['GridRow']
export type GridCell = components['schemas']['GridCell']
export type ParameterRow = components['schemas']['ParameterRow']
export type ScenarioOut = components['schemas']['ScenarioOut']

export type StatementGridOut = components['schemas']['StatementGridOut']

export type AiRecordOut = components['schemas']['AiRecordOut']
export type AiRecordDetailOut = components['schemas']['AiRecordDetailOut']
export type ConfirmOut = components['schemas']['ConfirmOut']
export type RevenueProposalOut = components['schemas']['RevenueProposalOut']

export type BacktestOut = components['schemas']['BacktestOut']
export type DeviationOut = components['schemas']['DeviationOut']
export type BacktestYearOut = components['schemas']['BacktestYearOut']

export type FindingOut = components['schemas']['FindingOut']
export type BrainReindexOut = components['schemas']['BrainReindexOut']
export type BrainValidateOut = components['schemas']['BrainValidateOut']
export type BrainSweepOut = components['schemas']['BrainSweepOut']
export type ReversalVerdictOut = components['schemas']['ReversalVerdictOut']

export type ClaimOut = components['schemas']['ClaimOut']
export type ClaimDetailOut = components['schemas']['ClaimDetailOut']
export type EvidenceOut = components['schemas']['EvidenceOut']
export type EffectOut = components['schemas']['EffectOut']
export type ClaimLinkOut = components['schemas']['ClaimLinkOut']
export type DecidedEffectOut = components['schemas']['DecidedEffectOut']
export type ImpactRowOut = components['schemas']['ImpactRowOut']

export type BridgeApplyOut = components['schemas']['BridgeApplyOut']

/** Both scenario kinds this build's screens switch between. */
export const SCENARIO_KINDS = ['base', 'best', 'worst'] as const
export type ScenarioKindT = (typeof SCENARIO_KINDS)[number]
