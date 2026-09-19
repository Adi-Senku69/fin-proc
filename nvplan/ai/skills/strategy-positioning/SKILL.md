---
name: strategy-positioning
description: How to state a position as for whom, against what alternative, on what dimension, with the proof - each claim tagged, landing as a decision record. Read before drafting or revising a positioning statement.
---

# Positioning: the method

Choosing which dimension to fight on, and which alternative to define yourself against, is
judgment: two competent people can pick different, both-defensible positions from the same market
facts (PLATFORM.md §8). This is a skill, and its output only counts here as claims that carry a
provenance tag and land in `brain/` - a punchy tagline with no evidence behind it is not a
positioning statement, it is copywriting.

## 1. What the statement must name

1. **For whom** - the specific customer segment, not "everyone". Link
   `brain/knowledge/users/segments.md` if a matching segment exists.
2. **Against what alternative** - the specific competitor, category, or status quo (including
   "doing nothing" / a manual process) that a prospect would otherwise choose.
3. **On what dimension** - the one axis of comparison you are claiming to win on (speed, cost,
   trust, integration depth, ...), not a list of five.
4. **The proof** - the evidence that the dimension claim is true today, not aspirational.

## 2. Procedure

1. Read `brain/knowledge/market/landscape.md` and any competitor files under
   `brain/knowledge/market/competitors/` for the alternative you are positioning against.
2. State the four elements above in one sentence each; resist adding a second dimension - a
   position that wins on everything is not a position.
3. For the proof, prefer a fact a prospect could verify (a benchmark, a customer quote, a metric)
   over an internal opinion. If the proof is a number, it must come from an existing engine
   figure (`(computed, <derivation-key>)`) or `nvplan/strategy/sizing.py` - never invented.
4. If the "against what alternative" claim rests on a competitor's stated or observed behavior
   that the environmental scan or an existing competitor file already covers, cite that instead
   of re-describing the competitor from scratch.

## 3. Provenance

Every claim - segment, alternative, dimension, proof - carries exactly one tag from the closed
enum, PLATFORM.md §4.1: `[ingestion/<path>](../ingestion/<path>)`,
`[source/<path>](../source/<path>)`, `(stakeholder-verbal, <name>, <YYYY-MM-DD>)`,
`(intuition, <role>, <YYYY-MM-DD>)`, `(industry-knowledge)`, `(chat, no artifact)`,
`(computed, <derivation-key>)`. A claim with no tag, or more than one, is an orphan and the file
is rejected - positioning gets no house exception because it "is just a sentence".

## 4. Where it lands

A positioning statement is a decision record, `brain/decisions/YYYY-MM-DD-<slug>.md`
(schema: `brain/decisions/_SCHEMA.md`): `## Context` (why reposition, or why position now),
`## Options considered` (other dimensions or alternatives you weighed and rejected),
`## Decision` (the for-whom / against-what / on-what-dimension statement), `## Why`,
`## Evidence` (the tagged proof and the tagged alternative/segment claims), `## Explicitly NOT
doing` (dimensions or segments you are deliberately not claiming), `## What would reverse this`
(a competitor move, a metric threshold, or a date that would force a repositioning),
`## Remaining ambiguities`. If the proof is still unverified rather than observed, open a
hypothesis (`brain/hypotheses/<feature-slug>.md`, viability or value risk) instead of stating it
as fact.
