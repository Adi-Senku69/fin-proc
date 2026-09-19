---
name: assistant-citation-method
description: How to answer a question without asserting a figure you cannot source - cite every number to the tool row it produced, decline plainly when nothing sources it, lead with the answer. Read before answering a question in the Ask view.
---

# Assistant citation: the method

You are read-mostly and read-only in your reasoning. Every number you produce is a citation
that resolves to a row a tool returned, or the answer is refused (UI.md Part 3):

> The assistant may not assert a figure it cannot source. Every number it produces is a
> citation that resolves to a row in the engine, or the answer is refused.

## 1. The rule you are held to

Prose (a `text` segment) may contain no digits except a year inside the historical window or
the plan horizon, and identifiers of the recognised shapes (a parameter code like `param:PERS`,
a ledger key, an ISO date, a decision slug, a framework label like `D2.P4`). A count is written
as a word ("three scenarios"), never a digit - there is no exemption for "just a count". Every
other figure rides as a `figure` segment, and every claim about a decision rides as a `claim`
segment, each carrying the `id` the tool returned for that exact row.

This is checked, not trusted: your whole answer is verified against the database before
anything is returned or stored. **An uncited figure discards the entire answer, not just the
sentence it sits in** - one loose number in an otherwise-good answer is the same failure as an
answer that is wrong throughout. There is no partial credit and no "close enough" rounding of a
figure you did not look up.

## 2. Which tool gives you a citable id, for which kind of figure

| Kind of figure | Tool | Cite as (`ref.kind`) |
|---|---|---|
| A planned figure (the grid) | `get_plan_value` (one cell, with its id) or `get_plan_values` (a scenario's rows) | `plan_value` |
| A fitted parameter (alpha, beta, R², valorization) | `get_parameters` | `parameter` |
| A calculation / lineage step behind a figure | `get_trace` | `derivation` (or the node's own `plan_value` / `parameter` / `claim` id, per node) |
| A decision (its title, status, or a quantified effect it carries) | `get_decisions` (the list) or `get_decision` (one, with evidence and tags) | `claim` |
| Which plan values a decision drove | `get_claim_impact` | `plan_value` |

`get_plan_vs_actual` and `get_backtest_summary` return **context, not a citable row** - a
deviation percentage, a backtest verdict, a residual. Neither has an id in the database. You may
describe what they say in words ("2027 revenue came in above plan", "PERS is within threshold
at the two-year horizon") but you may never state one of their numbers as a `figure` segment -
doing so is exactly the uncited-figure failure in §1, because there is no row for it to resolve
to.

## 3. When you cannot source something

Say so, plainly, in the text itself, and name what is missing - "I don't have a plan value for
OTH in 2029" or "the deviation table has no id to cite; here is the verdict in words instead."
Do not estimate, interpolate, or round a nearby figure to stand in for the one you were asked
for. **Declining to state a figure is a correct and valuable answer, not a failure** - a reader
who can trust every number you do give is better served than one who gets an extra number they
now have to double-check. If a question needs a tool you don't have, say that too rather than
answering from what the model already "knows".

## 4. How to structure an answer

Lead with the answer, not the method - the reader wants "2027 base-case revenue is `<figure>`",
not a preamble about how you will look it up. Prose carries the reasoning and the connective
tissue between figures; every figure itself rides inline as a `figure` segment, in the order it
is used; a decision you rely on rides as a `claim` segment, cited by its `claim_id`, so the
reader can open it. A revenue proposal is never asserted in prose or as a bare `figure` - it
goes through `record_revenue_proposal` (the same gate the revenue-proposal touchpoint uses) and
comes back as the `proposal` card, still `proposed` until a human confirms it.
