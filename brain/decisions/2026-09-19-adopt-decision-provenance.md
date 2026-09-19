# Decision: Adopt a markdown-plus-index decision-provenance system for the brain

## Status
decided

## Date
2026-09-19

## Context
The finance module already refuses to store a number without a derivation. Product
and strategy decisions had no equivalent: rationale lived in Slack threads, PR
descriptions, and people's memory, none of it queryable and most of it un-recoverable
within a quarter. PLATFORM.md proposes extending the same non-null-provenance rule
from arithmetic to judgment, via a markdown tree (`brain/`) that is authoritative and
a database index that is derived and rebuildable.

## Options considered
1. Keep decisions in ad hoc Slack threads and PR descriptions, unindexed.
2. Adopt a wiki (Notion/Confluence) with no structural enforcement of citations.
3. A git-native markdown tree under `brain/`, with a closed provenance-tag enum
   enforced both at write time (a hook) and at ingest time (a validator), indexed
   into the same kind of SQLAlchemy tables the finance module already uses.

## Decision
Option 3: `brain/` markdown files are authoritative; `provenance`/`brainkit` parse,
validate, and ingest them into a derived index. Every evidence bullet must carry
exactly one tag from the closed enum in PLATFORM.md §4.1, checked twice — a
write-time hook and an ingest-time validator — so a disabled hook can never let an
unsourced claim into the index.

## Why
Options 1 and 2 both let a claim exist with no traceable source, which is exactly
the failure mode the finance module's `derivation_id` foreign key was built to
prevent on the numeric side. Reusing that pattern for judgment costs one closed enum
and one validator, and buys every downstream decision a durable "why," including the
ones that later feed a plan override (PLATFORM.md §7).

## Evidence
- Undocumented product decisions are routinely re-litigated months later because no
  one can reconstruct why the original call was made, once the people who made it
  have moved on or simply forgotten  (industry-knowledge)
- We ourselves lost the rationale for an earlier pricing call inside of two months —
  nobody on the current team could say why the number was chosen, only that it was
  (intuition, PM, 2026-09-19)

## Explicitly NOT doing
- Building a dedicated web UI for browsing the brain in this phase — markdown files
  reviewed via git are the interface for P1  (intuition, PM, 2026-09-19)
- Vector search or embeddings over the brain — the file-plus-index design exists
  specifically to avoid needing them for a corpus this size  (industry-knowledge)

## What would reverse this
If, 90 days after `brainkit.ingest` is in daily use, fewer than half of newly shipped
decisions carry a complete evidence trail, or the write-time hook's false-positive
rate on legitimate writes exceeds 20% (measured by how often a human overrides it),
we would reconsider the schema's strictness or the enforcement mechanism.

## Remaining ambiguities
Where exactly the line sits between an `ingestion/` synthesis and a `source/` direct
citation is a judgment call for borderline artifacts (a short verbatim quote pasted
into a longer note, for instance). We are deferring a firmer rule until we have real
files to look at.
