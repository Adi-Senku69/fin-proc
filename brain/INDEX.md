# brain/ — the decision memory

This tree is the **authoritative** record (PLATFORM.md §3). The `provenance`/`brainkit`
index tables are a derived cache, rebuildable at any time by re-ingesting these files.
Edit a fact here, then re-ingest — never patch the database directly.

## What lives where

- `decisions/` — one file per decision, `YYYY-MM-DD-<slug>.md`. Lifecycle
  `pending → decided → superseded` (PLATFORM.md §4.3). Never edited in place once
  `decided`; a reversal is a new file that supersedes the old one.
- `hypotheses/` — one file per feature, `<feature-slug>.md`. Risk-area blocks
  (value/usability/feasibility/viability/other), each with its own evidence and
  lifecycle `open → supported|refuted → superseded`.
- `ingestion/{interviews,meetings,market,adhoc}/` — records synthesized from raw
  material. The strongest provenance tag (`[ingestion/...]`) points here.
- `source/` — raw artifacts (transcripts, docs). Never rewritten. The `[source/...]`
  tag points here directly when synthesis would be ceremony.
- `knowledge/` — living reference: `strategy.md`, `product/`, `users/`, `market/`,
  `org/`. Not evidence-bearing itself; decisions and hypotheses cite `ingestion/`,
  `source/`, or the parenthetical tags, not `knowledge/` pages.
- `_examples/` — fixtures for tests and onboarding, clearly marked as such. Never a
  real record.
- `_SCHEMA.md` in every collection — the format contract for that collection, checked
  by `brainkit.validate` and the write-time hook.

Every bullet under an evidence heading carries exactly one provenance tag from the
closed enum in PLATFORM.md §4.1, or the file is rejected — at write time by the hook
in `.claude/hooks/validate_brain_file.py`, and again at ingest time by
`brainkit.ingest`, so a disabled hook can never let an unsourced claim into the index.
