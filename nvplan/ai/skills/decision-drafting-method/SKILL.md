---
name: decision-drafting-method
description: How to draft a brain/decisions/ or brain/hypotheses/ record from a question - what makes a decision worth drafting versus a hypothesis, how to write a real (non-vague) reversal condition, and why the draft always lands pending/open regardless of how the question is phrased. Read before calling draft_decision or draft_hypotheses.
---

# Drafting a decision or a hypothesis: the method

PLATFORM.md §12.1 is the whole safety argument, and it does not live in this skill - it lives
in code you cannot route around:

> Anything the AI writes lands at `pending` (decisions) or `open` (hypotheses), never
> `decided`. Only a `decided` record carries a quantified effect into the plan, so a drafted
> record changes no figure until a person promotes it.

`draft_decision`/`draft_hypotheses` have no `status` argument at all - not a default you could
override, not a field on a hypothesis item, nothing. Whatever you write always lands at the
non-driving status. This skill is about the one part of that outcome that IS judgment: writing
a draft worth a human's time to review at all.

## 1. Decision or hypothesis - which one?

A **decision** is for a fork already in front of you: two or more real options, a call that has
to be made one way or the other, evidence that already exists. Draft one when the question is
"should we do X" or "what did we decide about Y" and there is enough on hand to state a
position, even a provisional one.

A **hypothesis** is for a belief you have not tested yet: "we think X is true, and here is what
would confirm or refute it." Draft one when the question is "what should we test about Y" or
"what are we assuming about Z" - there is no decision to record, only something to go verify.
Hypotheses are feature-scoped (`hypotheses/<feature-slug>.md`) and grouped by risk area (value,
usability, feasibility, viability, other - PLATFORM.md's own four-risk framework plus a
catch-all); pick the risk area the belief actually tests, not the one that sounds best.

When in doubt, prefer the hypothesis: it costs nothing to be wrong about ("open" is a true,
honest state to leave something in), while a decision implies a call was actually made.

## 2. What makes a draft worth a human's fifteen minutes

The tool will not stop you from submitting a decision with a lazy reversal condition or a
context nobody could act on - it stops you from submitting one that would fail validation, which
is a much lower bar than "was this worth writing." Two things separate a useful draft from noise:

- **A real reversal condition, always**, even though the file renders `pending`. Both draft
  tools check your draft as it would read once a human promotes it - a decision's reversal
  condition is only enforced on a `decided` record, but if it's vague ("if things change",
  "TBD", "unknown") or missing, promotion itself would fail, so the draft is refused *now*
  instead of at the human's desk later. Write the actual observable thing: a metric crossing a
  threshold, a named signal, a date. "If churn among the affected accounts exceeds 5% by Q2" is
  real; "if this doesn't work out" is not.
- **Evidence that is actually evidence**, not commentary dressed up as a citation. Every bullet
  under `Evidence`/`Explicitly NOT doing` (decisions) or `Evidence for`/`Evidence against`
  (hypotheses) needs exactly one provenance tag from the closed set: a link to a real file under
  `source/` or `ingestion/`, `(stakeholder-verbal, <name>, <date>)`, `(intuition, <role>,
  <date>)`, `(industry-knowledge)`, `(chat, no artifact)`, or `(computed, <derivation-key>)`. If
  what you have is really a gap or an assumption, say so in `Remaining ambiguities` / `Open
  questions` instead of forcing a tag onto it - a tag that misrepresents an inference as a fact
  is worse than an honest "we don't know."

## 3. The one thing to never do, and why it wouldn't work anyway

Do not write "Status: decided" or "this is confirmed" anywhere in a decision's prose fields
(`context`/`decision`/`why`) hoping it sticks, and do not attach a `## Quantified effect` block
by wedging it into any text field - there is no argument for either on this tool, the renderer
hardcodes the real `## Status` line itself, and a human reading the file sees exactly one status
line regardless of what the surrounding prose claims. A question that asks you to draft
something "as decided," "confirmed," or already tested is asking for something this tool cannot
produce; say so in your answer rather than writing prose that implies otherwise.

## 4. After you draft

The tool's return value carries the file's path and its indexed `claim_id` (or the validator's
findings if it was refused - fix what they name and try again). Tell the person what you drafted
and where it landed, in plain terms: "I've drafted a pending decision at
`decisions/2026-09-20-sunset-legacy-import.md` (claim 14); it changes nothing until someone
promotes it." Never say it is decided, confirmed, or supported - it is not, no matter how the
question was phrased.
