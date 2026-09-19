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
| `POST /brain/ingest` | `{files_seen, ingested, skipped_unchanged, rejected: [path], findings: [Finding]}` |
| `GET /brain/validate` | `{errors: [Finding], warnings: [Finding], clean: bool}` without ingesting |
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
   ingest the brain, apply decided effects, then a link straight to the figure the decision moved.

### Rules

- **Never invent a number.** Every figure on screen comes from an endpoint. No arithmetic in JavaScript
  beyond formatting.
- **Provenance is visible, not implied.** An evidence row always renders its tag. A figure whose path is
  `decided` or `ai_proposed` is visually marked as such in the grid.
- **The illustrative flag is never hidden**, per the finance specification.
- Readable at 1280 wide, degrades to a single column below 900. Dark and light both legible.
- No emoji as interface elements; text and shape carry meaning so it stays legible when projected.

## Phases

| Phase | Work | Done when |
|---|---|---|
| U0 | The seven endpoints above, with pydantic models and tests | Each returns the documented shape; impact resolves a decision to the figures it moved |
| U1 | The page and its seven views | The demo view drives the whole loop from an empty database and a figure traces back to its decision |
| U2 | Polish: keyboard navigation, deep-linkable hash state, print stylesheet | Deferred until U0 and U1 are exercised by hand |
