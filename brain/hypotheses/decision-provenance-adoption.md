# Hypotheses — decision-provenance-adoption

## Meta
- Feature: decision-provenance-adoption (brain/knowledge/product/features/decision-provenance-adoption.md not yet written)
- Created: 2026-09-19
- Last updated: 2026-09-19

## Value risk
### H-V1: PMs will tag evidence instead of treating the schema as friction
- **Origin:** proactive
- **Confidence:** medium
- **Evidence for:**
  - Our own team adopted the finance module's non-null derivation rule and kept
    using it despite the extra friction on every plan value  (intuition, PM, 2026-09-19)
- **Evidence against:**
  - Schema-enforced metadata in other tools we've used tends to fill up with
    placeholder tags under deadline pressure  (industry-knowledge)
- **Status:** open
- **Open questions / caveats:**
  - We don't yet know the write-time hook's false-positive rate in real use.

## Usability risk
### H-U1: The closed provenance-tag enum is expressive enough for real PM evidence
- **Origin:** proactive
- **Confidence:** medium
- **Evidence for:**
  - The seven tag forms covered every evidence source we needed while drafting the
    two worked decision examples for this platform  (intuition, PM, 2026-09-19)
- **Evidence against:**
  - Verbal evidence not attributable to one named stakeholder ("the support team
    says...") doesn't fit any of the seven forms cleanly  (industry-knowledge)
- **Status:** open
- **Open questions / caveats:**
  - Whether "the support team says" needs its own tag form, or should be split into
    per-person stakeholder-verbal rows, is unresolved.
