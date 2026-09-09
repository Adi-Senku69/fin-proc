---
name: env-scan-54-positions
description: How to work the 6x9 environmental framework (54 positions) for NewVision's five P&L categories - materiality rubric, what counts as a source, when and how to write an external note. Read before an environmental scan.
---

# Environmental scan: working the 54-position framework

The framework is 6 domains (D1..D6, PESTEL-like) x 9 positions each. Every position carries
the P&L categories it can affect (`affects`: REV, MAT, EXT, PERS, OTH). `get_env_framework`
returns the full structure; the user message lists the positions to investigate. Always name
positions by their ids ("D2.P4 Wage growth"), never by number alone.

## 1. Procedure

1. Read the company context and the existing external notes in the user message
   (`get_external_notes` if you need the full text). Existing notes are not re-recorded;
   refer to them by id ("see note 3").
2. Walk the requested positions domain by domain. For each one ask:
   - Is anything changing here over the planning horizon (2026-2030)?
   - Which of the affected categories does it hit, in which year(s)?
   - Roughly how strongly (share of revenue, percentage points on a cost rate, one-off k EUR)?
3. Rate materiality with the rubric in section 2. Skip immaterial positions silently: a short
   list of well-argued flags is worth more than a complete one.
4. For every **material** (medium or high) position write exactly one external note with
   `record_external_note` (section 4). Low-materiality positions may appear in the structured
   result but do not get a note.
5. Return the structured result: one `flagged` entry per position you kept (domain, position,
   materiality, reasoning, source) and a two-to-three-sentence summary.

## 2. Materiality rubric

Judge the effect on the affected category relative to its size, over the horizon:

| materiality | rule of thumb | example |
|---|---|---|
| high | >= 2% of revenue, or >= 1 percentage point on a cost rate of the largest category (PERS), or a discrete event (contract end, regulation) that changes a category by >= 5% in one year | wage rounds +1.5pp above the valorization rate on personnel (~80% of costs) |
| medium | 0.5-2% of revenue, or a cost effect of 1-5% in one category, or a trend that needs a plan assumption even if the magnitude is uncertain | public-sector IT budgets growing ~3% p.a. supporting a framework agreement |
| low | below 0.5% of revenue and below 1% of any category, or no plausible change in the horizon | regional policy with no known programme touching the company |

If you cannot put a number on it, say so and default to medium at most.

## 3. What counts as a source

- A source is something a controller could check: a named regulation, a contract mentioned in
  an existing note, a published statistic, or a figure from the actuals/parameters tools.
- No web tool is wired in this proof of concept. When a finding rests on general knowledge or
  on an assumption, set `source` to `"assumption/illustrative"` and phrase the note as an
  assumption ("assumed +4-5% p.a.").
- Never present an assumption as a fact, and never invent figures that look like data.

## 4. Writing the note

One `record_external_note` call per material position, with:

- `domain` and `position`: the framework ids and names, e.g. `"D2 Economic"`, `"D2.P4 Wage growth"`.
- `category_code`: the single category it hits (REV, MAT, EXT, PERS, OTH) or omit if it hits several.
- `year`: the first plan year affected.
- `text`: self-contained - what is changing, when, magnitude if known, and the source. A good
  note can be read alone months later: "Collective wage rounds in the sector point to +4-5%
  p.a. for 2026-2027, above the 2.8% valorization of the fixed personnel part
  (source: assumption/illustrative)."

Do not write a note for a position that an existing note already covers; cite that note id
in your reasoning instead. Duplicate notes dilute the revenue-proposal step that reads them.
