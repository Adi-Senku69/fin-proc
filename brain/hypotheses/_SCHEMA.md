# Hypothesis file — schema

> Read this before writing or editing a hypothesis file. Its placeholder rows are a
> template, not real evidence, and are exempt from error-level findings.

Hypotheses are **feature-scoped**: one file per feature, named `<feature-slug>.md`.
Risk areas are **value, usability, feasibility, viability, other** — `other` is for
risks that don't fit the canonical four (regulatory, brand, partnership, security,
internal-political, ...).

```markdown
# Hypotheses — <feature-name>

## Meta
- Feature: <link to knowledge/product/features/<slug>.md, if it exists>
- Created: YYYY-MM-DD
- Last updated: YYYY-MM-DD

## Value risk
### H-V1: <one-sentence belief>
- **Origin:** proactive | data-derived (from <source>)
- **Confidence:** low | medium | high
- **Evidence for:**
  - <claim>  <provenance-tag>
- **Evidence against:**
  - <claim>  <provenance-tag>
- **Status:** open | supported | refuted | superseded
- **Open questions / caveats:**
  - <what we don't know that would change confidence>

## Usability risk
### H-U1: ...

## Feasibility risk
### H-F1: ...

## Viability risk
### H-B1: ...

## Other risk
### H-O1: <name the risk type in the heading>
```

## The hard rules

1. **No orphan evidence.** Every bullet under `**Evidence for:**` and
   `**Evidence against:**` carries exactly one tag from the closed enum in
   PLATFORM.md §4.1 (the same seven forms as decisions — see
   `../decisions/_SCHEMA.md`). A bullet with no tag, or more than one, is an orphan
   and the file is rejected.
2. **Evidence rows are claims, not commentary.** A row under `Evidence for:` /
   `Evidence against:` asserts something the world told us. Gaps, inferences, and
   things we don't yet know go under `Open questions / caveats:`, which carries no
   tag requirement.
3. **Status is one of the four lifecycle states** (PLATFORM.md §4.3): `open` →
   `supported` or `refuted` → `superseded`. Nothing else — no compound or
   file-specific statuses.
4. **Aggregate rows are not evidence.** "N=3 accounts, mixed sentiment" is a claim
   about the evidence, not a claim from the world; it cannot honestly wear a single
   tag, so it belongs under `Open questions / caveats:`.

## Pre-save checklist

1. Count the bullets under every `**Evidence for:**` / `**Evidence against:**` list
   in the file. Count the tags in those same bullets. The two numbers must match.
2. Every tag is one of the seven canonical forms, spelled exactly.
3. Every path-typed tag resolves from this file's location (one `..` up).
4. Every `**Status:**` line is `open`, `supported`, `refuted`, or `superseded`.
5. Nothing that is really a gap, an inference, or an aggregate sits under an
   Evidence heading.
