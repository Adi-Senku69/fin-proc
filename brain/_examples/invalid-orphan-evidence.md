<!--
INTENTIONAL TEST FIXTURE — this file is deliberately invalid. It exists so
tests/test_brainkit_ingest.py and tests/test_brainkit_parse.py can exercise the
orphan-evidence rejection path. It is not a real decision, it should never be cited
from anywhere, and `brainkit.ingest` must never turn it into a Claim row. It lives
under brain/_examples/, which brainkit.validate treats as fixture space (findings
here are reported at warning severity at most, so validate_tree over the real brain
root stays clean) — the strict-rejection behaviour itself is exercised in
tests/test_brainkit_ingest.py against a copy of this content placed in a real
collection directory, where the orphan-evidence finding keeps its full error
severity.
-->

# Decision: Ship the weekly digest without a batching option

## Status
decided

## Date
2026-01-01

## Context
A fixture context paragraph, not a real decision.

## Options considered
1. Ship as-is.
2. Add a batching toggle.

## Decision
Ship as-is.

## Why
Fixture reasoning text.

## Evidence
- Users churn faster when notifications arrive in daily batches instead of real time

## Explicitly NOT doing
- A configurable batching window

## What would reverse this
if things change

## Remaining ambiguities
None — this is a fixture.
