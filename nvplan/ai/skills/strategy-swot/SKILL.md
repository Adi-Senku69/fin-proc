---
name: strategy-swot
description: How to write a SWOT where every strength, weakness, opportunity and threat is sourced - an unsourced entry is inadmissible - and it lands as a decision record. Read before drafting or revising a SWOT.
---

# SWOT: the method

Rating your own strengths and reading the market's threats is judgment - two competent people
given the same facts can weigh them differently (PLATFORM.md §8). So this is a skill, and every
entry it produces must be expressible as a tagged claim landing in `brain/`, not a bullet list
that only reads well.

## 1. The rule for every quadrant

**An unsourced entry is inadmissible.** Strengths and Weaknesses look inward (product, team,
cost structure, retention); Opportunities and Threats look outward (market, competitors,
regulation, macro). Every one of the four lists is evidence-only: if you cannot tag it, it does
not go in the SWOT, full stop - it goes under `## Remaining ambiguities` instead.

## 2. Procedure

1. Read `brain/knowledge/strategy.md`, `brain/knowledge/product/metrics.md`, and
   `brain/knowledge/market/landscape.md` / `trends.md` for what is already on record.
2. **Do not duplicate the environmental scan.** `env-scan-54-positions` already covers the
   external-factor analysis (6 domains x 9 positions, PESTEL-like) that Opportunities and Threats
   would otherwise re-derive. Pull Opportunities/Threats entries from existing external notes or
   env-scan findings (cite the note/finding id in your reasoning) rather than re-running that
   scan here; write a fresh entry only for something the scan does not cover (e.g. an
   internally-observed strength or weakness).
3. For Strengths/Weaknesses, ground each entry in a metric (`(computed, <derivation-key>)`), a
   stakeholder statement, or documented product/org knowledge - never a vibe.
4. Rank within each quadrant by materiality (borrow the rubric in `env-scan-54-positions` section
   2 if the entry concerns revenue or cost exposure); a short, well-sourced list beats a long one.

## 3. Provenance

Every entry in all four quadrants carries exactly one tag from the closed enum, PLATFORM.md §4.1:
`[ingestion/<path>](../ingestion/<path>)`, `[source/<path>](../source/<path>)`,
`(stakeholder-verbal, <name>, <YYYY-MM-DD>)`, `(intuition, <role>, <YYYY-MM-DD>)`,
`(industry-knowledge)`, `(chat, no artifact)`, `(computed, <derivation-key>)`. Zero tags or more
than one makes the entry an orphan and the file is rejected - there is no quadrant that is exempt.
A quantified entry must come from `nvplan/strategy/sizing.py` or an existing engine figure, never
an invented number.

## 4. Where it lands

A SWOT is a decision record, `brain/decisions/YYYY-MM-DD-<slug>.md`
(schema: `brain/decisions/_SCHEMA.md`): `## Context`, `## Options considered` (what strategic
response each quadrant suggests), `## Decision` (the SWOT itself, four labeled lists),
`## Why`, `## Evidence` (every tagged entry, restated), `## Explicitly NOT doing`,
`## What would reverse this`, `## Remaining ambiguities` (anything you could not source, and any
S/W/O/T candidate you dropped for that reason).
