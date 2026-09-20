# Demo and testing interface — contract

Status: contract, 2026-09-19. Serves `PLATFORM.md` (the platform) and `PLAN.md` (the finance module).

## Purpose and the stack decision

A surface for **testing and demonstrating** the whole loop: a decision with evidence moves money, and
any figure opens to show why. Not a production product interface.

**No build step.** A single page served by the existing FastAPI app, vanilla JavaScript modules and
hand-written CSS, no CDN and no bundler. Node is installed on this machine but is deliberately unused:
a demo that needs `npm install` before it renders is a worse demo, and an offline-capable page survives
a room with bad wifi. The finance specification names React with Vite and Tailwind; that remains the
upgrade path and costs nothing later, because the interface talks only to the JSON endpoints below.

## Part 1 — endpoints to add (`nvplan/api/app.py`)

The brain and the bridge currently have none. All responses are JSON with pydantic models.

| Route | Returns |
|---|---|
| `POST /brain/reindex` | `{files_seen, indexed, skipped_unchanged, rejected: [path], findings: [Finding]}` |
| `GET /brain/validate` | `{errors: [Finding], warnings: [Finding], clean: bool}` without reindexing |
| `GET /brain/claims?kind=&status=` | `[{id, kind, slug, title, status, date, path, has_effect}]` |
| `GET /brain/claims/{id}` | one claim plus `evidence: [{section, text, tag_kind, tag_raw, target_path, resolved}]`, `reversal_condition`, `effect`, `links: [{relation, other_slug}]` |
| `GET /brain/effects` | decided quantified effects: `[{claim_id, decision_slug, decision_title, category_code, year, value, unit, decided_on}]` |
| `POST /bridge/apply` | runs the plan with the decided effects as overrides. `{plan_run, applied: [year], shadowed: [claim_id]}` |
| `GET /brain/claims/{id}/impact` | **the reverse of the trace.** Which plan values this claim drove: `[{plan_value_id, scenario_kind, category_code, year, value, path}]`, plus the default it displaced where recorded |

`Finding` serialises as `{path, line, code, message, severity}`. Paths are relative to the repository
root so the interface can show them without leaking absolute paths.

The impact route is the one genuinely new idea. The trace answers "what produced this figure". Impact
answers "what did this decision cost", which is the platform's whole pitch stated in the other
direction.

## Part 2 — the page (`nvplan/api/static/`)

`GET /` serves `index.html`. One page, a left rail of views, no routing library; the URL hash selects
the view so a state is linkable.

### Views

1. **Plan** — the grid: categories down, years across, a tab per scenario. The illustrative badge is
   always visible when any actual carries that label. Each parameter row shows alpha, beta, R² and the
   valorization rate. Clicking any cell opens the trace beside it.
2. **Trace** — the lineage tree, collapsible, each node showing its formula, parameters, inputs and
   source label. A node sourced from a decision shows the claim block: title, status, decided date,
   every evidence row with its provenance tag rendered as a chip, and the reversal condition. A node
   sourced from an AI proposal shows the stored prompt verbatim, its rationale and its confirmer.
3. **Brain** — decisions and hypotheses with status chips. Selecting one shows its evidence rows with
   tag chips, its reversal condition, its quantified effect, and its impact. A validation banner shows
   the tree's findings; a file with an error is marked and explained.
4. **Statements** — profit and loss, balance sheet and cash flow, with the consistency check result
   shown as a pass or fail per year rather than asserted in prose.
5. **Backtest** — error per category and horizon, verdict coloured against the threshold, and the
   per-window parameter table.
6. **AI records** — each record's touchpoint, status, model, the literal prompt, and the per-call audit
   log with real token usage so cache effectiveness is visible.
7. **Demo** — the loop as buttons, in order, each reporting what it did: ingest actuals, run the plan,
   reindex the brain, apply decided effects, then a link straight to the figure the decision moved.

### Rules

- **Never invent a number.** Every figure on screen comes from an endpoint. No arithmetic in JavaScript
  beyond formatting.
- **Provenance is visible, not implied.** An evidence row always renders its tag. A figure whose path is
  `decided` or `ai_proposed` is visually marked as such in the grid.
- **The illustrative flag is never hidden**, per the finance specification.
- Readable at 1280 wide, degrades to a single column below 900. Dark and light both legible.
- No emoji as interface elements; text and shape carry meaning so it stays legible when projected.

### Design language (adopted from the reference React app, 2026-09-20)

The client's own reference implementation (a separate repo: a Vite/React app built on Tailwind CSS,
`apps/web` in the finance spec's proof-of-concept) has a considered visual language even though this
build cannot use its stack. **No build step** (above) still stands — nothing here pulls in React,
Tailwind, or any bundler. What moved is the *design*, read out of the reference's components and
re-expressed as hand-written CSS custom properties and plain selectors in `nvplan/api/static/app.css`.

**What was taken, concretely:**

- **Palette.** The reference's Tailwind slate/indigo/emerald/amber/rose/violet/sky scale, ported as
  hex values on named tokens (`--bg`, `--fg`, `--accent`, `--ok`, `--warn`, `--error`, `--ai`, `--info`,
  each with a `-bg`/`-border` pair) rather than utility classes. The mapping follows the reference's own
  usage exactly where it has one: amber = illustrative/notice (`IllustrativeBadge`, `ForecastNotice`
  is sky instead — folded into `--info`), violet = `ai_proposed` (`DerivationPanel`'s
  `PATH_BADGE_CLASSES`), emerald = confirmed/good (the `ProposalScreen` confirm step). Two concepts
  this app has that the reference doesn't — `decided` and `illustrative` as first-class chip kinds —
  alias the closest reference hue (`--decided` = `--ok`/emerald, `--illustrative` = `--warn`/amber)
  instead of inventing new ones, so the total vocabulary stays the same six hues the reference uses.
- **Shape.** Rounded-full pill badges, rounded-md/-lg cards with a 1px border and a soft `shadow-sm`,
  a segmented-control look for the scenario/statement/backtest-basis tabs (a quiet track, the active
  option raised on a white chip) copying `ScenarioTabs.tsx`/`BacktestScreen.tsx` exactly. Prompt,
  formula and other code-ish boxes became the reference's own quiet `bg-slate-50` monospace block
  instead of the previous dark "terminal" look.
- **Type/spacing rhythm.** Muted, uppercase, letter-spaced "eyebrow" labels for section headings and
  table headers (`text-xs font-semibold tracking-wide text-slate-500 uppercase` in the reference); a
  clear light/dark hierarchy between body text and headings/figures (`--fg` vs `--fg-strong`).
- **Left rail, not a top nav.** The reference's `Nav.tsx` is a horizontal underline-tab bar for seven
  screens. This build keeps its existing **vertical** rail — a deliberate, documented deviation, not
  an oversight: eight views (including Brain and Ask) plus a persistent health badge crowd a
  single-row nav much sooner, and the vertical rail already had the correct responsive fallback
  (collapses to a horizontal scroller under 900px). The rail's *colours* were still ported: a light
  panel, slate body text, and an indigo active state replace the previous dark sidebar.
- **Dark mode.** The reference has none (a Tailwind build with zero `dark:` variants anywhere in
  `apps/web/src`). This build's existing `prefers-color-scheme: dark` block predates this pass and
  was kept — not "taken" from the reference, and not newly invented for it either — because dropping
  a working accessibility mode nobody asked to lose isn't what porting a design language means. Its
  palette was retuned to the same indigo/emerald/amber/rose/violet/sky mapping at dark-appropriate
  values so both modes read as one system.
- **Provenance stamp kept distinct.** This app's own prior design decision — a tag chip (the literal
  `PLATFORM.md §4.1` provenance string) renders as a bold, dark "stamp," separate from ordinary
  badges — doesn't exist in the reference and was kept on purpose (`--tag-bg`/`--tag-fg`, decoupled
  from the now-light `--code-bg`/`--code-fg`), so lightening the code/formula boxes didn't flatten
  the one piece of chrome this app relies on to make provenance impossible to miss.

**Brain and Ask** have no counterpart in the reference (it has no decision/evidence model and no
chat). Both were designed to sit inside the ported language rather than alongside it: Brain's status
chips reuse the same `chip-status`/`chip-path` vocabulary and hue mapping as every other status
surface in the app (Plan's marked cells, the AI records table, Trace's claim block); Ask's figure and
claim chips are the same pill shape in the accent/decided colours, styled as inline citations ("part
of the sentence, not a footnote") rather than as a new component family, and its refusal panel reuses
the bold two-tone treatment `error`/`error-bg` already established for `.error-panel`.

Every view was re-verified against the live API after this pass (see the phase table's own "done
when" criteria) — endpoints, response shapes and the click-through-to-derivation interaction are
unchanged; only `app.css` (values, plus a few additive rules — segmented tabs, `--*-border` tokens,
card shadows) changed.

## Phases

| Phase | Work | Done when |
|---|---|---|
| U0 | The seven endpoints above, with pydantic models and tests | Each returns the documented shape; impact resolves a decision to the figures it moved |
| U1 | The page and its seven views | The demo view drives the whole loop from an empty database and a figure traces back to its decision |
| U2 | Polish: keyboard navigation, deep-linkable hash state, print stylesheet | Deferred until U0 and U1 are exercised by hand |

---

## Part 3 — the conversational view (internal)

Status: contract, 2026-09-19. **Internal demo only.** Not shown to Newvision, so it speaks in our own
category codes and needs no client branding. The illustrative labelling stays regardless.

### The premise, and the one hard constraint

A chat is the right shape for the question a product manager actually asks. A chat that states bare
numbers is not, because it reduces this product to a model wrapper and answers "where did that come
from?" with "the assistant said so" — the exact failure the architecture exists to prevent.

> **The assistant may not assert a figure it cannot source.** Every number it produces is a citation
> that resolves to a row in the engine, or the answer is refused.

This is the same discipline `figures.check_explanation` already applies to deviation explanations,
generalized. It turns the chat from something that hides the provenance system into something that
demonstrates it, which is the more persuasive demo anyway.

### Answer shape

```python
Segment =
  | {"type": "text",   "text": str}
  | {"type": "figure", "label": str, "value": float, "unit": str,
     "ref": {"kind": "plan_value" | "parameter" | "derivation" | "claim", "id": int}}
  | {"type": "claim",  "claim_id": int, "title": str, "status": str}

Answer = {"segments": [Segment], "proposal": Proposal | None,
          "ai_record_id": int, "usage": {...}}
```

### Verification, before anything is returned or stored

1. Every `figure` segment's `ref` must resolve to a real row, and its `value` must match that row
   within 0.05 in the stored unit. A mismatch or a dangling reference rejects the whole answer.
2. **Backstop scan.** A number-like token appearing in a `text` segment is allowed only when it is a
   year inside the historical window or the plan horizon, or an ordinal like "three scenarios". Any
   other loose figure rejects the answer, because it is by definition uncited.
3. A rejected answer persists nothing and returns 422 with what failed, exactly as a bad deviation
   explanation does. The interface says the assistant produced an unsourced figure and was refused.
   **That refusal is a feature and should be visible, not hidden.**

### Tools — read-only, plus the existing gate

Reuse `nvplan/ai/tools.py` where it fits and add brain-side equivalents: the plan grid, a single plan
value with its id, a trace, the fitted parameters, plan versus actual, the backtest summary, the
decision list, one decision with its evidence and tags, and a claim's impact. Nothing may write except
the existing proposal path, which still lands as `proposed` and still needs a human to confirm.

A revenue proposal arrives as a card with confirm and reject, not as prose. Confirming runs the
existing gate and cascade untouched.

### Endpoint and view

`POST /assistant/ask` takes `{question, scenario_kind?}` and returns the answer. Each call costs real
money, so the view shows the token usage it already records per call.

The `Ask` view renders segments in order: text as prose, figures as inline chips that open the trace,
claims as chips that open the decision. A proposal renders as a card. A refusal renders as a refusal.
