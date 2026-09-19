# Decision: Plan personnel cost from the regression-derived trend, not a flat headcount guess

## Status
decided

## Date
2026-09-19

## Context
The five-year plan needs a personnel-cost (PERS) trajectory. The naive approach is a
flat headcount-times-average-salary guess re-typed every planning cycle, with no
link back to what actually happened historically. The finance engine already
produces a regression-derived trend for PERS as part of its category maths, with a
`derivation` row backing every parameter it fits.

## Options considered
1. A flat headcount-driven manual estimate, re-entered each cycle.
2. The regression-derived PERS trend the finance engine already computes, cited by
   its derivation key.
3. A blended estimate that averages 1 and 2.

## Decision
Option 2: the product plan's personnel-cost assumption pins itself to the finance
engine's `param:PERS` derivation rather than a retyped number.

## Why
A retyped number (options 1 and 3, partially) can silently drift from what the
regression actually found and nobody would notice until the two disagreed at
statement time. Citing the derivation key directly means opening this decision
always reaches the exact calculation and its inputs, not a paraphrase of them — the
bridge PLATFORM.md §7 describes in the "money informs decisions" direction.

## Evidence
- The PERS regression's fitted trend explains the trailing eight quarters of actual
  personnel cost well enough to plan against directly, per the finance engine's own
  parameter fit  (computed, param:PERS)
- Regression-fitted cost trends are the standard planning input over
  manually re-estimated headcount costs specifically because they don't silently
  drift from what actually happened  (industry-knowledge)

## Explicitly NOT doing
- Re-deriving or overriding the PERS parameter for planning purposes — planning
  consumes the finance engine's fit, it doesn't compute its own
  (intuition, PM, 2026-09-19)

## What would reverse this
If the trailing-eight-quarter R-squared for the PERS regression drops below 0.6 at a
future recompute, or planned headcount diverges from the regression's implied cost
trend by more than 15% for two consecutive quarters, we would revisit this and
likely fall back to a manually adjusted estimate for that cycle.

## Remaining ambiguities
This decision assumes the finance engine's `param:PERS` derivation stays available
and stable across recomputes; how a *superseding* recompute of that same parameter
should flow back into this decision's evidence is left to Phase P2 (the bridge).
