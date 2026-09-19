# NewVision AI-supported planning PoC (`nvplan`)

A planning engine for five P&L categories in which **every number is traceable to its
derivation in one click**, with AI limited to three advisory, read-only touchpoints behind a
human confirmation gate. Built from
[`AI-Supported Planning — PoC Architecture & 3-Week Implementation Plan.pdf`](./AI-Supported%20Planning%20%E2%80%94%20PoC%20Architecture%20%26%203-Week%20Implementation%20Plan.pdf)
(PerfektWerk for Newvision Software GmbH). The build plan is [PLAN.md](./PLAN.md); the
hand-checked figures are in [VERIFICATION.md](./VERIFICATION.md).

> **All figures in this repository are ILLUSTRATIVE.** `data/illustrative/` is generated
> dummy data (`nvplan-gen-data`, seed 42), not Newvision figures. Every actual carries
> `source_label=ILLUSTRATIVE`; the label travels into every trace, grid and API response
> (`illustrative: true`) and is never hidden. Real actuals, the real account mapping and
> Newvision's own P&L-to-balance-sheet mapping are the open items of the PDF.

## Architecture in ten lines

1. `data/illustrative/` - actuals 2016-2025 (k EUR), investment plan, BS mapping, control table, external notes, 54-position environmental framework.
2. `nvplan/db/` - nine SQLAlchemy tables (SQLite); `plan_value`, `statement_line`, `parameter` carry a NOT NULL FK to `derivation`, so a number without a derivation cannot exist.
3. `nvplan/core/regression.py` - OLS per cost category: `Cost = alpha + beta * Revenue`, R² surfaced, valorization `v` = mean growth of the fixed part.
4. `nvplan/core/projector.py` - `PlanCost_t = alpha*(1+v)^(t-t0) + beta*PlanRevenue_t`; best / base / worst are three revenue paths through the same engine.
5. `nvplan/core/statements.py` - P&L -> balance sheet (config mapping) -> cash flow (delta of BS movements); consistency asserted every run.
6. `nvplan/core/{backtest,deviation}.py` - MAPE / RMSE per category and horizon against the 5-8 % thresholds; plan-vs-actual arithmetic with a beta split.
7. `nvplan/services/planning.py` - the only writer of numbers; append-only; one `derivation` per number, parents linked.
8. `nvplan/services/trace.py` - click a number: the lineage tree down to the source rows (`render_trace` prints the PDF's layout).
9. `nvplan/ai/` - deepagents touchpoints (env scan, revenue proposal, deviation explanation); read-only tools, rationale required, the literal prompt stored in `ai_record.prompt_text`.
10. `nvplan/services/gate.py` + `nvplan/api/` - human confirm / reject; confirming reruns the plan with the proposal as the base revenue. FastAPI exposes it all as JSON.

## Install and test

```bash
uv sync                 # Python 3.13, creates .venv
uv run pytest -q        # whole suite, no network needed (AI tests use scripted fake models)
```

## Run the demo

```bash
uv run nvplan-demo                  # fresh nvplan_demo.db, prints the whole story (~1 s, scripted AI)
uv run nvplan-demo --live           # opt into the real model; spends real tokens, takes minutes
uv run python scripts/demo.py       # same thing
```

The demo ingests the illustrative actuals, runs the plan, prints the base grid and the
parameter table with R², the full trace of *Personnel costs 2028 base*, the backtest report,
then runs the environmental scan and a revenue proposal for 2027, has "C. Andres" confirm it
through the gate, reruns, prints the changed REV / PERS rows and the PERS 2027 trace with the
AI block (confirmer + verbatim prompt), checks the balance sheet balances, runs the deviation
explanation on the backtest year 2025, and closes with the row counts per table.

## Run the API

```bash
uv run nvplan-serve                          # http://127.0.0.1:8000, docs at /docs
uv run nvplan-serve --db sqlite:///nvplan_demo.db --port 8000
NVPLAN_DB_URL=sqlite:///other.db uv run nvplan-serve
```

| Route | What it returns |
|---|---|
| `GET /health` | status, DB URL, illustrative flag, row counts |
| `POST /ingest` | loads `data/illustrative/actuals.csv` into `actual` (or a CSV/XLSX sent as the raw request body, `?source_label=`, or `?path=` on the server); seeds the illustrative external notes once (`?with_notes=false` to skip) |
| `POST /plan/run` `{created_by, label_suffix?}` | runs the plan (three scenarios); ids and counts |
| `GET /plan/grid?scenario_kind=base[&scenario_id=]` | the plan grid: rows REV, MAT, EXT, PERS, OTH, DEPR x years, cell = `{value, path, plan_value_id, ai_record_id}`, plus the parameter rows (alpha, beta, R², v; g for REV), scenario label, illustrative flag |
| `GET /plan/scenarios` | all scenarios (id, kind, label, created_by, created_at) - the scenario switch |
| `GET /statements/{kind}?statement=pl\|bs\|cf[&scenario_id=]` | statement lines as a grid with `statement_line_id` per cell; `bs` also carries the consistency check |
| `GET /trace/plan-value/{id}` / `GET /trace/statement-line/{id}` | the lineage tree (`?format=text` for the printed layout) - click any number |
| `GET /ai/records[?touchpoint=&status=]`, `GET /ai/records/{id}` | AI records incl. the verbatim `prompt_text`, `response_text`, rationale, status, confirmer - the prompt viewer |
| `POST /ai/records/{id}/confirm` `{confirmed_by}` | the human gate: confirms and reruns the plan with the proposal (409 if not `proposed`) |
| `POST /ai/records/{id}/reject` `{rejected_by}` | rejects; nothing enters the figures (409 if already decided) |
| `POST /ai/env-scan` `{positions_subset?}` | touchpoint 1: scans the framework, writes `external_note`s (`source=ai_scan`) |
| `POST /ai/revenue-proposal` `{scenario_kind, year}` | touchpoint 2: default value taken from the latest plan (valorized path), proposal stored `status=proposed` (422 if it breaks a control-table rule) |
| `POST /ai/deviation-explanation` `{scenario_kind, year}` | touchpoint 3: explanation citing the plan-vs-actual figures |
| `GET /backtest[?window_len=5]` | MAPE / RMSE per category and horizon, both bases, fits per window, markdown report, thresholds |
| `GET /deviation?scenario_kind=base[&year=]` | plan vs actual for the years that have both (none for 2026+ on the dummy data) |
| `GET /deviation/backtest-year?year=2025` | fits on the five years before `year`, projects `year`, compares with the actual - without persisting |

## Real model vs scripted fakes

The AI routes and the demo use the real model only when `ANTHROPIC_API_KEY` is set
(model `claude-opus-5`, see `AI_MODEL` in `nvplan/config.py`; `langchain-anthropic` via
deepagents). Without a key the API answers `503` on the three touchpoint routes and the demo
falls back to the scripted fakes in `nvplan/ai/fake.py`, saying so in its output. Tests inject
fakes through `app.state.model_factory` and never touch the network. The deterministic engine
(plan, trace, statements, backtest, deviation, gate) does not depend on the AI layer at all.

```bash
# put ANTHROPIC_API_KEY in .env (auto-loaded), or export it
uv run nvplan-ai-check              # one cheap call: is the model reachable?
uv run nvplan-demo --live           # real env scan, proposal and deviation explanation
uv run pytest -m live               # the live test suite (deselected by default)
```

## Layout

```
nvplan/config.py        paths, category codes, regression window, plan years, model name
nvplan/data/generate.py dummy data generator (nvplan-gen-data)
nvplan/db/              models (nine tables), session / seeding
nvplan/ingest/          CSV / XLSX -> actual rows
nvplan/core/            ledger, regression, projector, depreciation, statements, deviation, backtest
nvplan/services/        planning (writer), trace (lineage), gate (confirm / reject)
nvplan/ai/              tools, prompts, schemas, deepagents wiring, fakes (see nvplan/ai/README.md)
nvplan/api/             FastAPI app (nvplan-serve), queries, demo (nvplan-demo)
tests/                  golden regression, consistency, gate, AI guardrails, API, demo
```

**Context management and skills**: the AI agents run behind deepagents middleware (large tool-result eviction, clearing of old tool results, token-triggered summarization, Anthropic prompt caching) and load one `SKILL.md` per touchpoint from `nvplan/ai/skills/`. Every model call's literal request is logged to `ai_record.call_log_json` and served by `GET /ai/records/{id}`. Details and tuning in `nvplan/ai/README.md`.
