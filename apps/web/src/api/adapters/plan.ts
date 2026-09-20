import { api } from '../client'
import type { PlanGridOut } from '../types'

export type ScenarioKind = 'base' | 'best' | 'worst'
export const SCENARIO_KINDS: readonly ScenarioKind[] = ['base', 'best', 'worst']

/**
 * `GET /plan/grid` already returns a grid shaped the way the screen wants it (categories down,
 * years across, each cell carrying its own `path`/`plan_value_id`) — see `GridRow`/`GridCell` in
 * `../types.ts`, generated from `nvplan/api/schemas.py`. This adapter is the one seam between
 * that wire shape and the screen: every other file downstream of it imports `PlanGridOut` from
 * here, never straight off `../client`, so a future reshaping only touches this file.
 */
export function fetchPlanGrid(scenarioKind: ScenarioKind) {
  return api.get<PlanGridOut>(`/plan/grid?scenario_kind=${encodeURIComponent(scenarioKind)}`)
}
