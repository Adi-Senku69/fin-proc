# Ingestion records — schema

Files here are records **we synthesized** from raw material in `../source/` — an
interview writeup, a meeting summary, a cross-customer synthesis, a market scan. An
ingestion file is what a decision or hypothesis cites with the
`[ingestion/<path>](../ingestion/<path>)` tag (PLATFORM.md §4.1), the highest-trust
provenance form because it names both the synthesis and, inside it, the raw artifact
it came from.

Subdirectories, by the kind of raw material behind them:

- `interviews/` — user and customer interview synthesis.
- `meetings/` — internal or stakeholder meeting notes and decisions surfaced there.
- `market/` — competitor scans, analyst notes, market research synthesis.
- `adhoc/` — anything that doesn't fit the other three.

## Conventions

```markdown
# <one-line title of what was synthesized>

## Source
[source/<kind>/<file>.md](../source/<kind>/<file>.md)  <!-- or: no artifact, verbal only -->

## Date
YYYY-MM-DD

## Synthesis
<!-- What we learned, in our own words. This is the text a decision or hypothesis
     paraphrases when it cites this file with an [ingestion/...] tag. -->
```

An ingestion file is not itself required to carry provenance tags on its body text —
it *is* the provenance. It should, however, link back to the `source/` artifact it
came from wherever one exists, so the chain is walkable in both directions.
