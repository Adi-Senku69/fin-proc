# Decision: Sunset the legacy CSV import feature

## Status
decided

## Date
2026-09-20

## Context
The legacy CSV-only import path predates the current importer and is used by a
shrinking pocket of accounts that never migrated to the supported XLSX/API import. It
doubles our importer surface area and consumes a fixed slice of support and
engineering time every quarter. Removing it will cost us a small, plannable amount of
revenue from the accounts that don't migrate in time, rather than an unplanned amount
discovered later at actuals time.

## Options considered
1. Keep supporting the legacy CSV import indefinitely.
2. Sunset it on a fixed date with 90 days' advance notice to affected accounts, and
   plan for the resulting revenue loss explicitly.
3. Sunset it immediately with no notice period.

## Decision
Option 2: sunset the legacy CSV import on 2026-12-19 (90 days out), with advance
customer notice, and plan the 2027 revenue path net of the accounts we expect not to
migrate.

## Why
Option 1 keeps a second import code path alive indefinitely for a shrinking user base,
at a real and growing maintenance cost. Option 3 is needlessly abrupt and would
forfeit goodwill we can buy cheaply with 90 days' notice. Option 2 costs a specific,
modeled amount of revenue instead of an unplanned one discovered after the fact.

## Evidence
- Support tickets tagged "csv-import" have not grown for six consecutive quarters
  even as total account count has grown, and legacy-importer accounts as a share of
  total signups have fallen every quarter for the past two years, per the support
  lead's own quarterly tracking  (stakeholder-verbal, Head of Support, 2026-09-15)
- Sunsetting a low-usage legacy integration path ahead of a scheduled, well-notified
  migration deadline is standard practice for converting an unbounded, unplanned
  support cost into a small, planned one-time revenue cost  (industry-knowledge)

## Explicitly NOT doing
- Building a CSV-to-XLSX auto-migration tool for the affected accounts — the cheaper
  path is the 90-day notice period plus manual migration help for the handful of
  accounts that ask for it  (intuition, PM, 2026-09-20)
- Sunsetting any other legacy import path in the same change — this decision covers
  the CSV import path only  (chat, no artifact)

## What would reverse this
If, in the 60 days following the 2026-12-19 sunset, more than 10 currently-active
accounts have not completed migration, or realised 2027 Q1 revenue from
previously-legacy-importer accounts falls more than 25% short of the -518.9 kEUR
(23,418.9 -> 22,900.0 kEUR) effect modeled below, we would offer a paid temporary
extension of the legacy path to the remaining accounts rather than hold the date.

## Remaining ambiguities
Whether any affected account also carries a bundled services contract that would drag
down a different category (EXT) on departure is not modeled here; this decision prices
the REV effect only.

## Quantified effect
- category: REV
- year: 2027
- value: 22900.0
- unit: kEUR
