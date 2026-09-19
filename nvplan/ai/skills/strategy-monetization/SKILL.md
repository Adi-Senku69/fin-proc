---
name: strategy-monetization
description: How to lay out pricing/packaging options with their revenue logic and what must be true for each to work, sizing every number through nvplan/strategy/sizing.py. Read before proposing a pricing or packaging option.
---

# Monetization: the method

Which pricing model fits, and what must be true for it to pay off, is judgment - two competent
people can back different, defensible packages from the same market read (PLATFORM.md §8). This
is a skill: what it must produce is not a slide of options but claims that carry a provenance tag
and land in `brain/` as a decision, so the packaging choice is checkable later.

## 1. What each option must state

For every pricing/packaging option you propose:

1. **The mechanic** - per-seat, usage-based, flat-tier, one-off, ... - stated plainly.
2. **The revenue logic** - how the mechanic turns into money at a given adoption level; if you
   size it, use `nvplan/strategy/sizing.py` (section 2), never a number typed by hand.
3. **What must be true for it to work** - the load-bearing assumption (a willingness-to-pay level,
   a conversion rate, a usage pattern) stated as a claim or, if untested, as an assumption to be
   turned into a hypothesis.

## 2. Every number goes through the sizing module or the engine

`nvplan/strategy/sizing.py` is the only place a TAM/SAM/SOM-shaped figure is computed:

```python
from nvplan.strategy.sizing import SizingInput, market_size

total = SizingInput(population=..., penetration=..., price=..., label="<option>", source_tag="(industry-knowledge)")
result = market_size(total, serviceable_share=..., obtainable_share=..., ledger=ledger)
```

Cite the resulting figure as `(computed, <derivation-key>)` using `result.derivation_keys` once
the run has been ingested. A revenue estimate that is not a `market_size` output and not an
existing engine figure (a `plan:*`/`param:*` key already in the derivation ledger) is not a
number this skill is allowed to produce - never invent one; state the gap instead of estimating
it by feel.

## 3. Procedure

1. Read `brain/knowledge/strategy.md` and `brain/knowledge/market/landscape.md` for the pricing
   context already on record (existing price points, competitor pricing, prior decisions).
2. Draft two or three options, not one - a monetization decision needs an "against what" the way
   positioning does.
3. For each, run or cite the sizing figure (section 2) and state the load-bearing assumption.
4. Rank the options by what would have to be true, not by revenue alone - the option with the
   biggest number and the shakiest assumption is not automatically the recommendation.

## 4. Provenance

Every claim - mechanic, revenue logic input, load-bearing assumption stated as fact - carries
exactly one tag from the closed enum, PLATFORM.md §4.1: `[ingestion/<path>](../ingestion/<path>)`,
`[source/<path>](../source/<path>)`, `(stakeholder-verbal, <name>, <YYYY-MM-DD>)`,
`(intuition, <role>, <YYYY-MM-DD>)`, `(industry-knowledge)`, `(chat, no artifact)`,
`(computed, <derivation-key>)`. A claim with no tag, or more than one, is an orphan and the file
is rejected.

## 5. Where it lands

The chosen package is a decision record, `brain/decisions/YYYY-MM-DD-<slug>.md`
(schema: `brain/decisions/_SCHEMA.md`): `## Context`, `## Options considered` (every option you
laid out, mechanic and revenue logic), `## Decision` (the one chosen), `## Why`, `## Evidence`
(the tagged claims, including every `(computed, <derivation-key>)` sizing figure used),
`## Explicitly NOT doing` (the options you rejected and why), `## What would reverse this` (a
revenue or adoption threshold, or a date), `## Remaining ambiguities`, and, only if the decision
is `decided` and drives a `REV` figure, `## Quantified effect` (category, year, value, unit -
PLATFORM.md §7.1). Every "what must be true" assumption you did not already confirm becomes its
own entry in `brain/hypotheses/<feature-slug>.md` (schema: `brain/hypotheses/_SCHEMA.md`) under
viability risk, `**Status:** open`, until evidence moves it.
