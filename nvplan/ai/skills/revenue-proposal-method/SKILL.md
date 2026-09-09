---
name: revenue-proposal-method
description: The arithmetic recipe for a revenue proposal - start from the valorized default, quantify flagged notes, respect the control table, cite note ids, propose the default when nothing justifies a change. Read before proposing a revenue value.
---

# Revenue proposal: the method

The engine already has a deterministic revenue path, the **valorized default** for the target
year. Your job is to decide whether a flagged external factor justifies a different figure and,
if so, which one - with the arithmetic written out. All cost categories cascade from revenue
(PlanCost = alpha*(1+v)^(t-t0) + beta*PlanRevenue), so the number moves the whole plan once a
human confirms it.

## 1. Procedure

1. Read the revenue actuals (`get_actuals` with `category_code="REV"`): trend and recent growth
   rate. Read the flagged external notes (`get_external_notes`) and the control table
   (`get_control_table`).
2. Start from the default. List the notes that concern revenue (category REV) in or before the
   target year. Ignore notes that only touch cost categories.
3. For each relevant note, quantify its effect on the target year as a share of revenue and a
   timing fraction (section 2). If a note gives no magnitude, state the assumption you make.
4. Combine the effects, compute the proposal, round to the nearest 10 k EUR, and check it
   against the control table (section 3).
5. Record it with `record_revenue_proposal(year, proposed_value, rationale, cited_note_ids)`.
   If the tool returns an error, fix exactly what it names and call it again; never work
   around it (do not drop the citation, do not reword to hide a bound breach).
6. Return the structured result with the same year, value, rationale, note ids, and the
   factors you used.

## 2. The arithmetic (worked example)

Default 2027: 21 900 k EUR.

- Note 1: "major client contract, ~9% of revenue, ends Q2 2027". Effect: -9% of revenue for the
  half of the year after Q2 -> -9% * 0.5 = -4.5% on 2027.
- Note 3: "framework agreement ramps up from H2 2026, ~+2% of revenue at full run-rate".
  Effect: fully in run-rate in 2027 -> +2.0% on 2027, but if the default's growth trend already
  contains the ramp-up (check the actuals: is 2026 growth above the historical rate?), do not
  count it twice; say which choice you made.

Combined: -4.5% + 2.0% = -2.5%. Proposal: 21 900 * (1 - 0.025) = 21 352.5 -> **21 350**.
Deviation from default: -2.5% (within the 25% bound). Cited notes: [1, 3].

Write the rationale exactly like that: default, each note with its effect and timing fraction,
the combined percentage, the multiplication, the rounding, the bound check.

## 3. Control-table rules

- `max_deviation_from_default_pct`: |proposal - default| / default * 100 must not exceed it.
  If your arithmetic lands outside the bound, do not clip silently - propose the bound value
  and say the evidence points further ("capped at -25%; note 1 alone implies -30%").
- `must_cite_note`: `cited_note_ids` must contain at least one **existing** note id that
  drives the proposal. Cite ids, not text; only cite notes you actually used.
- `requires_human_confirmation`: the proposal stays `proposed` until a human confirms; say so
  in the rationale's last sentence.

## 4. When nothing justifies a change

If no note gives a concrete, quantifiable reason for the target year, propose the **default
itself**, cite the note(s) you examined and rejected, and say why they do not move the figure
("note 2 concerns 2029 and is outside the target year"). Never invent a factor to justify a
change, and never change the figure for a rounder number.
