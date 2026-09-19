---
name: strategy-porters-five-forces
description: How to rate each of Porter's five forces with the evidence behind the rating and what would change it, without re-deriving the environmental scan. Read before drafting or revising a five-forces analysis.
---

# Porter's five forces: the method

Rating "threat of new entrants" as low versus medium is a judgment call two competent analysts
can make differently from the same facts (PLATFORM.md §8) - a skill, not code. Its output is
worthless as a page of ratings unless each one is a tagged claim landing in `brain/`.

## 1. The five forces, and what each rating needs

For each force - **competitive rivalry**, **threat of new entrants**, **threat of substitutes**,
**bargaining power of suppliers**, **bargaining power of buyers** - state three things:

1. **Rating**: low / medium / high.
2. **The evidence for the rating** - one or more tagged claims (section 3).
3. **What would change it** - a specific, observable condition (a competitor entering, a supplier
   concentration shift, a substitute reaching price/feature parity), not "if the market changes".

## 2. Procedure

1. Read `brain/knowledge/market/landscape.md`, `trends.md`, and any files under
   `brain/knowledge/market/competitors/`.
2. **Do not duplicate the environmental scan.** `env-scan-54-positions` already covers the
   macro/external drivers (regulatory, economic, technological, ...) that feed several of these
   forces, especially new entrants and substitutes. Cite the relevant existing external note or
   env-scan finding by id instead of re-running that analysis; add a fresh claim here only for
   something the 54-position scan does not carry (e.g. a specific competitor's pricing move, a
   named supplier's contract terms).
3. Rate each force from the cited evidence, not from a template default - a force with no
   evidence gets no rating; put it under `## Remaining ambiguities` instead of guessing "medium".
4. If a force affects a P&L category with a number attached (e.g. supplier power showing up as a
   cost-rate risk), the number must come from an existing engine figure or
   `nvplan/strategy/sizing.py`, never an invented figure.

## 3. Provenance

Every "evidence for the rating" claim carries exactly one tag from the closed enum, PLATFORM.md
§4.1: `[ingestion/<path>](../ingestion/<path>)`, `[source/<path>](../source/<path>)`,
`(stakeholder-verbal, <name>, <YYYY-MM-DD>)`, `(intuition, <role>, <YYYY-MM-DD>)`,
`(industry-knowledge)`, `(chat, no artifact)`, `(computed, <derivation-key>)`. Zero tags or more
than one on a claim makes it an orphan and the file is rejected; a rating with no tagged evidence
at all does not go in the analysis.

## 4. Where it lands

Five forces is a decision record, `brain/decisions/YYYY-MM-DD-<slug>.md`
(schema: `brain/decisions/_SCHEMA.md`): `## Context`, `## Options considered` (strategic postures
the ratings suggest), `## Decision` (the five ratings, each with its "what would change it"),
`## Why`, `## Evidence` (every tagged claim behind every rating), `## Explicitly NOT doing`,
`## What would reverse this` (the sharpest single trigger across all five forces - a real,
checkable condition), `## Remaining ambiguities` (any force you could not rate for lack of
evidence).
