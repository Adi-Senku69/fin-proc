# Decision record — schema

> Read this before writing or editing a decision file. This document is a template:
> its own placeholder rows (`<claim>`, `<not-doing>`, ...) are not real evidence and
> are exempt from error-level findings (PLATFORM.md §9) — but every real decision
> file is held to the letter of the rules below.

Filename: `YYYY-MM-DD-<slug>.md` (the decided date, or the date opened if pending).

```markdown
# Decision: <one-line statement>

## Status
pending | decided | superseded

## Date
YYYY-MM-DD

## Context
<!-- 2-4 sentences: the problem, the fork in the road. -->

## Options considered
1.
2.
3.

## Decision
<!-- What we picked. Empty only while pending. -->

## Why
<!-- The actual reasoning. Empty only while pending. -->

## Evidence
- <claim>  <provenance-tag>

## Explicitly NOT doing
- <not-doing>  <provenance-tag>

## What would reverse this
<!-- A metric threshold, a named signal, or a date. Never "if things change". -->

## Remaining ambiguities
<!-- What we still don't know. Never evidence. -->

## Quantified effect
<!-- Optional. Valid only when ## Status is `decided`. -->
- category: <REV|MAT|EXT|PERS|OTH>
- year: <YYYY>
- value: <number>
- unit: kEUR
```

### Quantified effect (PLATFORM.md §7.1) — optional, decision-drives-money direction

A `decided` decision may carry one `## Quantified effect` block, a four-bullet
`key: value` list:

- `category` — one of the five planning category codes (`REV`, `MAT`, `EXT`, `PERS`,
  `OTH`; `nvplan.config.CATEGORY_CODES`).
- `year` — an integer inside the plan horizon (`nvplan.config.PLAN_YEARS`).
- `value` — a positive number.
- `unit` — must be `kEUR`, nothing else.

Rules:

1. **Decided only.** The block is valid only on a `decided` decision. Present on a
   `pending` or `superseded` decision, it is an `effect_on_undecided` error and the
   file is rejected under strict ingest, exactly like any other error-level finding.
2. **Structurally sound or rejected.** An unknown category code, a year outside the
   plan horizon, a non-numeric or non-positive value, a unit other than `kEUR`, or a
   missing key is a `bad_effect` error — the file is rejected the same way.
3. **Only `REV` currently drives the plan.** A structurally valid effect on any
   category other than `REV` parses and indexes (`Claim.effect_json`) but drives
   nothing downstream in Phase P2 — that fires `effect_not_wired`, a warning, not an
   error, so the file still ingests.
4. A decided decision's `REV` effect becomes the revenue override the planning run
   accepts (`bridge.effects.revenue_override`); every plan value it touches then
   traces back to this decision, its evidence, and the person who confirmed it.

## The hard rules (PLATFORM.md §4.2, §4.4)

1. **No orphan evidence.** Every bullet under `## Evidence` and `## Explicitly NOT
   doing` carries **exactly one** tag from the closed enum in PLATFORM.md §4.1 —
   `[ingestion/<path>](...)`, `[source/<path>](...)`,
   `(stakeholder-verbal, <name>, <YYYY-MM-DD>)`, `(intuition, <role>, <YYYY-MM-DD>)`,
   `(industry-knowledge)`, `(chat, no artifact)`, or `(computed, <derivation-key>)`.
   A bullet with zero tags, or more than one, is an orphan and the whole file is
   rejected. Path-typed tags must be written as markdown links and must resolve to a
   real file from this file's location.
2. **Commentary is not evidence.** Gaps, unknowns, and aggregate rows ("three
   customers, mixed sentiment") never go under `## Evidence` — they belong under
   `## Remaining ambiguities`, which carries no tag requirement because it asserts
   nothing.
3. **A decided decision states its reversal condition.** `## What would reverse
   this` must be a specific, observable condition — a metric threshold, a named
   signal, or a date. The literal phrases "if things change", "TBD", "unknown", and
   an empty section are all rejected.
4. **Decided is immutable.** A `decided` decision is never edited in place. To
   reverse it, write a new file with a new date and slug, set its status, and name
   the old file under `## Context` or `## Why`; the old file's `## Status` becomes
   `superseded`.

## Pre-save checklist

1. Count the bullets under `## Evidence` and `## Explicitly NOT doing`. Count the
   tags in those same bullets. The two numbers must match exactly.
2. Every tag is one of the seven forms above, spelled exactly.
3. Every path-typed tag is a working markdown link, and it resolves from this file.
4. `## Status` is one of `pending`, `decided`, `superseded` — nothing else.
5. If `## Status` is `decided`, `## What would reverse this` names a real,
   checkable condition, not a shrug.
6. The filename is `YYYY-MM-DD-<slug>.md`.
