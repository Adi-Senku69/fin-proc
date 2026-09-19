---
name: strategy-product-vision
description: How to state a product vision as who changes, what changes for them, and the observable signal that proves it - each claim tagged, each number sourced, landing as a decision record. Read before drafting or revising a vision statement.
---

# Product vision: the method

Two competent people looking at the same market and the same users can honestly land on
different visions - it is judgment, not arithmetic (PLATFORM.md §8). That makes this a skill, and
a skill's output is worthless here as free-standing prose: what it produces must be expressible
as claims that wear a provenance tag and land in `brain/` as a decision or a hypothesis.

## 1. What the vision must state

1. **Who changes** - the named user or segment. Link to `brain/knowledge/users/personas.md` or
   `segments.md` if a matching one exists there.
2. **What changes for them** - the before/after in one or two sentences, not a slogan.
3. **The observable signal** that would show it is working - a metric, an event, or a threshold,
   never "customers are happier".

## 2. Procedure

1. Read `brain/knowledge/strategy.md` and any decision it already points to; a new vision
   supersedes the old one rather than sitting alongside it (PLATFORM.md §4.3).
2. Draft the who / what-changes / signal triad above.
3. Source each claim (section 3). If the signal is a target you are proposing rather than a fact
   you observed, mark it explicitly as an assumption and open a hypothesis for it (section 4) -
   do not assert a target as if it were evidence.

## 3. Provenance - no exceptions for a vision

Every claim carries exactly one tag from the closed enum in PLATFORM.md §4.1:
`[ingestion/<path>](../ingestion/<path>)`, `[source/<path>](../source/<path>)`,
`(stakeholder-verbal, <name>, <YYYY-MM-DD>)`, `(intuition, <role>, <YYYY-MM-DD>)`,
`(industry-knowledge)`, `(chat, no artifact)`, `(computed, <derivation-key>)`. A claim with no
tag, or more than one, is an orphan and the whole file is rejected - a vision statement gets no
house exception.

**Never invent a number.** A quantified signal must come from an existing engine figure cited as
`(computed, <derivation-key>)` (a `plan:*` or `param:*` key that resolves in the derivation
ledger) or from `nvplan/strategy/sizing.py` (`market_size` - a TAM/SAM/SOM figure, cited the same
way once it is ingested). If no such figure exists yet, say the signal is a target to be
validated, not a fact.

## 4. Where it lands

A vision is a decision record, `brain/decisions/YYYY-MM-DD-<slug>.md`
(schema: `brain/decisions/_SCHEMA.md`). Fill: `## Context` (why a vision is needed now),
`## Options considered` (framings you rejected), `## Decision` (the who / what-changes / signal
triad), `## Why`, `## Evidence` (one tagged bullet per claim behind who/what-changes),
`## Explicitly NOT doing` (segments or changes you deliberately exclude, tagged),
`## What would reverse this` (an observable condition, never "if things change"),
`## Remaining ambiguities`. If the *signal* itself is the uncertain part, also open
`brain/hypotheses/<feature-slug>.md` (schema: `brain/hypotheses/_SCHEMA.md`) under whichever risk
area it belongs to (value, usability, feasibility, viability, other), so the assumption is tracked
to `supported` or `refuted` rather than left as an unexamined claim inside the vision.
