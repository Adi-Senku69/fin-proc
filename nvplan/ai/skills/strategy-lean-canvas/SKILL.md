---
name: strategy-lean-canvas
description: How to fill a lean canvas so every box either cites evidence or is explicitly marked an assumption to be tested, turning the untested boxes into hypothesis records. Read before drafting or revising a lean canvas.
---

# Lean canvas: the method

Which problem matters most, which channel will work, what customers will actually pay - two
competent people can disagree on all nine boxes from the same facts. That is judgment, not
arithmetic (PLATFORM.md §8), so this is a skill: its output only has value here as claims that
wear a provenance tag and land in `brain/`, never as a page of prose nobody can check.

## 1. The nine boxes

Problem, Customer Segments, Unique Value Proposition, Solution, Channels, Revenue Streams, Cost
Structure, Key Metrics, Unfair Advantage. For every box, write each line as either:

- an **evidenced claim** - something the world told us, tagged (section 3); or
- an **assumption to be tested** - said as such, and turned into a hypothesis (section 4), never
  left dressed up as fact.

A box with unlabeled prose that is neither is incomplete; do not fill it just to fill it.

## 2. Procedure

1. Read `brain/knowledge/strategy.md`, `brain/knowledge/users/personas.md` / `segments.md`, and
   `brain/knowledge/market/landscape.md` for what is already known.
2. For Revenue Streams and Cost Structure specifically, do not invent figures: pull a number from
   `nvplan/strategy/sizing.py` (`market_size`, for a TAM/SAM/SOM-shaped line) or from an existing
   engine figure; anything else on those two boxes is an assumption, not a claim, until it has a
   source.
3. For every other box, write the strongest evidenced claim you can, then explicitly separate out
   what you are *assuming* rather than know - vague optimism belongs in the assumption pile, not
   the evidence pile.
4. Do not re-run an external-factor scan here: `env-scan-54-positions` already covers PESTLE-like
   external drivers. If a box needs one, cite the existing external note or env-scan finding by
   id rather than re-deriving it.

## 3. Provenance

Every evidenced line carries exactly one tag from the closed enum, PLATFORM.md §4.1:
`[ingestion/<path>](../ingestion/<path>)`, `[source/<path>](../source/<path>)`,
`(stakeholder-verbal, <name>, <YYYY-MM-DD>)`, `(intuition, <role>, <YYYY-MM-DD>)`,
`(industry-knowledge)`, `(chat, no artifact)`, `(computed, <derivation-key>)`. A line with no tag
that is not clearly marked an assumption is an orphan and the file is rejected. A quantified line
must come from `nvplan/strategy/sizing.py` or an existing engine figure - never a number you made
up to make the box look complete.

## 4. Where it lands

The canvas as a whole is a decision record, `brain/decisions/YYYY-MM-DD-<slug>.md`
(schema: `brain/decisions/_SCHEMA.md`): `## Context` (why this canvas, why now), `## Decision`
(the canvas summarized box by box), `## Why`, `## Evidence` (every evidenced line, tagged),
`## Explicitly NOT doing`, `## What would reverse this`, `## Remaining ambiguities`. Every box you
marked "assumption to be tested" becomes its own entry in
`brain/hypotheses/<feature-slug>.md` (schema: `brain/hypotheses/_SCHEMA.md`), filed under the
matching risk area - Customer Segments and Problem are usually value risk, Channels usability,
Solution feasibility, Revenue/Cost viability - with `**Status:** open` until evidence moves it.
