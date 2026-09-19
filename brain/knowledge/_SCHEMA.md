# Knowledge base — schema

Living reference pages, not evidence-bearing records. `knowledge/` is where the
current understanding of strategy, product, users, market, and org gets written
down and kept current — it is what a decision's `## Context` points *at*, not what a
decision's `## Evidence` cites (evidence cites `ingestion/`, `source/`, or a
parenthetical tag; a `knowledge/` page is a summary, not a source).

Layout (PLATFORM.md §5):

- `strategy.md` — the current strategic frame.
- `product/metrics.md`, `product/roadmap.md`, `product/features/<slug>.md`
- `users/personas.md`, `users/segments.md`, `users/insights.md`
- `market/landscape.md`, `market/trends.md`, `market/competitors/<slug>.md`
- `org/team.md`, `org/rituals.md`, `org/tools.md`

## Conventions

Each page is free-form prose plus whatever structure helps; there is no closed
section list to validate here the way there is for decisions and hypotheses. The
one rule that carries over: don't smuggle unsourced claims into a knowledge page as
a way of dodging the evidence-tag requirement. A claim worth defending belongs in a
decision or hypothesis file, tagged, even if the knowledge page also restates it in
passing.

Update these pages in place — they are the "current understanding," not an
append-only log. When a page's claim is later contradicted, update the page and let
the decision or hypothesis that contradicted it be the durable record of *why*.
