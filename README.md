# NewVision (`nvplan`)

The quantification module of a product-management platform. Today it is a planning engine for
five P&L categories in which **every number is traceable to its derivation in one click**, plus a
markdown "brain" of decisions and hypotheses (`brain/`) that is indexed into SQLite and bridged to
the planning engine so a decision can move money and a figure can back a decision. The full
architecture contract, including how the two halves join, is `PLATFORM.md` - read that rather than
a paraphrase here. The finance module's own build plan is `PLAN.md`; its hand-checked figures are
in `VERIFICATION.md`; the demo/testing UI is specified in `UI.md`.

> **All figures in this repository are ILLUSTRATIVE.** `data/illustrative/` is generated dummy
> data (`nvplan-gen-data`, seed 42), not Newvision figures. Every actual carries
> `source_label=ILLUSTRATIVE`; the label travels into every trace, grid and API response
> (`illustrative: true`) and is never hidden. See `VERIFICATION.md` for exactly what has and has
> not been independently checked.

## This runs end to end with no API key

A deterministic, offline provider is the default for every AI-shaped code path. On a fresh clone,
`uv sync` -> generate data -> run the demo -> serve the API -> run the tests all work with **no
`ANTHROPIC_API_KEY`, no network call to Anthropic, and no cost**. This is a recent, deliberate
change (`nvplan/config.py`, knob `NVPLAN_AI_PROVIDER`, closed set `auto | live | deterministic`,
default `auto`):

- `auto` (the default) resolves to the real Anthropic client only when a credential is actually
  resolvable (`ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` in the environment, or a stored `ant` CLI
  profile) - deterministic otherwise. So on a clean checkout with no `.env`, everything is
  deterministic automatically.
- **Dropping a key into `.env` changes this.** Once a credential is present, every `auto`
  touchpoint (the three AI touchpoints and the assistant) resolves to the *real* model. That is
  by design for someone who wants live calls, but it means a `.env` with a working key is not
  inert.
- Opt into live calls deliberately with `NVPLAN_AI_PROVIDER=live` (still fails fast with no
  credential, never falls back silently), or rely on `auto` + a key in `.env`.
- **The one path that writes into `brain/`** - the environmental-scan touchpoint, which can draft
  a real markdown file under `brain/ingestion/market/` - has its own, separate knob,
  `NVPLAN_AI_BRAIN_WRITE_PROVIDER`, defaulting to `deterministic` regardless of `NVPLAN_AI_PROVIDER`
  or any key in `.env`. That is deliberate: a credential that happens to be sitting in `.env` for
  the other two touchpoints must not also make every developer's environmental scan quietly write
  a brain file drafted by a live model. Set it explicitly to `auto` or `live` if you want that too.

## Prerequisites and install

- [`uv`](https://docs.astral.sh/uv/) (tested with 0.11.7)
- Python >= 3.13 (`pyproject.toml`'s `requires-python`; `uv sync` installs a matching interpreter
  into `.venv` if you don't have one)

```bash
uv sync
```

Every command below is `uv run <thing>`, which resolves the project's console scripts (and its
`.venv`) regardless of your shell `$PATH` - no separate activation step needed.

## Quickstart

```bash
uv run nvplan-gen-data               # writes data/illustrative/* (actuals, investment plan, BS
                                      # mapping, control table, notes, 54-position env framework;
                                      # deterministic, seed 42)

uv run nvplan-demo                   # fresh nvplan_demo.db, ~1-2 s, deterministic/scripted AI by
                                      # default - prints the whole story end to end
uv run nvplan-demo --live            # opt into the real model instead: spends real tokens, takes
                                      # minutes, fails fast (exit 2) with no credential
uv run python scripts/demo.py        # identical to nvplan-demo (thin wrapper, same argv)

uv run nvplan-serve                  # http://127.0.0.1:8000 - the UI at / and API docs at /docs
uv run nvplan-serve --db sqlite:///nvplan_demo.db --port 8000
NVPLAN_DB_URL=sqlite:///other.db uv run nvplan-serve
```

Verified on this checkout: `nvplan-gen-data` wrote 8 files; `nvplan-demo` finished in 1.8 s with no
network access and exit code 0, printing the plan grid, a full trace, the backtest, an AI proposal
confirmed through the gate, and closing row counts per table; a server started with `nvplan-serve`
answered `200` on `/health`, `/`, `/docs` and `/plan/grid`.

**Heads up:** the demo's environmental-scan step is real B2 wiring, not a mock - when it flags a
position it drafts and indexes an actual file under `brain/ingestion/market/` (named
`<date>-env-scan-<ai_record_id>.md`), by default against the *real* `brain/` tree in this
checkout, even with the deterministic/offline model. That is intentional (`PLATFORM.md` §12.4),
not a side effect to suppress, but it does mean running `nvplan-demo` can leave a new untracked
file under `brain/` - check `git status brain/` after a demo run if you want to keep the tree
clean, or point `--db` at a scratch location and re-run to see it happen deliberately.

## Running the tests

```bash
uv run pytest -q          # whole suite except `live`; no network, no credential needed
uv run pytest -m live     # opt-in only: the live tests, which make real Anthropic calls
```

`pyproject.toml` sets `addopts = "-m 'not live'"`, so a plain `uv run pytest` never calls out; the
`live` marker (`tests/test_live_model.py`, 4 tests) is deselected by default and needs a real
`ANTHROPIC_API_KEY` to do anything when explicitly selected. Verified on this checkout: `627
passed, 4 deselected, 2 xfailed` with no credential in the environment.

## Verification / check console scripts

Each proves one thing end to end, deterministically, against a temporary database (and, where
noted, a temporary `brain/` tree) - no network call except where stated. This list is meant to
grow; append new entries below in the same form.

- `uv run nvplan-ai-check` - is the configured Claude model reachable? Prints the resolved AI
  configuration always; without a credential it prints the hint and exits 1 sending nothing; with
  one it makes exactly one cheap real call (or, with `--touchpoint {env-scan,revenue-proposal,
  deviation-explanation}`, runs one whole touchpoint for real against a temporary DB). This is the
  one check that talks to the network, and only when a credential is actually present.
- `uv run nvplan-bridge-check` - proves the P2 bridge both directions: a decided decision with a
  quantified effect recalculates the plan (personnel delta = beta * revenue delta), and a decision
  citing `(computed, <key>)` resolves to a real derivation row. Fresh temp SQLite DB, no network.
- `uv run nvplan-brain-write-check` - proves B2: the env-scan write path drafts and indexes a real
  `brain/ingestion/market/` record, and a decision citing it as evidence resolves. Fresh temp
  `brain/` tree and temp SQLite DB, no network.
- `uv run nvplan-brain-draft-check` - proves B3: a drafted decision drives no figure while
  `pending`, and drives the exact figure it names once a human promotes it by hand-editing the
  file (never through a writer function). Fresh temp `brain/` tree and temp SQLite DB, no network.
- `uv run nvplan-brain-sweep-check` - proves B4: the sweep finds a `decided` decision's reversal
  condition tripped against the real illustrative baseline plan, and reports it without mutating a
  single brain file or claim status. Fresh temp `brain/` tree and temp SQLite DB, no network.

All five were run against this checkout with no `ANTHROPIC_API_KEY` in the environment;
`nvplan-ai-check` correctly reported "NO CREDENTIAL" and exited 1, the other four exited 0.

## Configuration reference

Every value below is read from the environment at import (`nvplan/config.py`), so anything in
`.env` (git-ignored, mode 600; `.env.example` is the template) works the same as exporting it.

| Variable | Default | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` | unset | The credential. Its mere presence is enough to flip every `auto`-resolved touchpoint to live. |
| `ANTHROPIC_WORKSPACE_ID` | unset | Not a credential; forwarded as the `anthropic-workspace-id` header, needed only for an organization-level key. |
| `NVPLAN_AI_PROVIDER` | `auto` | `auto \| live \| deterministic`. Which provider the three AI touchpoints and the assistant resolve to. |
| `NVPLAN_AI_BRAIN_WRITE_PROVIDER` | `deterministic` | Same closed set; the env-scan's own, separate provider knob for the one path that can write into `brain/`. |
| `NVPLAN_AI_MODEL` | `claude-opus-5` | Model id handed to `ChatAnthropic`. |
| `NVPLAN_AI_EFFORT` | `high` | `low \| medium \| high \| xhigh \| max` reasoning effort. |
| `NVPLAN_AI_MAX_TOKENS` | `16000` | Output token cap per model call. |
| `NVPLAN_AI_BETAS` | (none) | Comma-separated Anthropic beta flags; escape hatch, unused by default. |
| `NVPLAN_DB_URL` | `sqlite:///nvplan.db` | SQLAlchemy URL `nvplan-serve` uses when `--db` is not given. |
| `NVPLAN_REGRESSION_METHOD` | `ols` | `ols \| joint`. Cost-regression method (see `VERIFICATION.md` §5.1/5.2). |
| `NVPLAN_VALORIZATION_DEGENERACY_SHARE` | `0.05` | Below this share of mean cost, `\|alpha\|` is treated as degenerate and the valorization rate is reported undefined. |
| `NVPLAN_JOINT_BETA_MAX` | `2.0` | Plausibility ceiling on the joint fit's variable rate; a converged fit above it falls back to OLS. |
| `NVPLAN_JOINT_VALORIZATION_MIN` / `_MAX` | `-0.5` / `0.5` | Plausibility band on the joint fit's valorization rate. |
| `NVPLAN_ASSISTANT_MAX_ATTEMPTS` | `3` | Correction attempts the assistant's `ask()` gets before `AnswerRejected` propagates. |

## Repo layout

```
nvplan/        the finance module: db models, regression/projection/statements, services
               (planning, trace, gate), the ai/ advisory layer, the FastAPI app and demo
provenance/    shared provenance core: tag enum + parser, lifecycle enums, index ORM models
brainkit/      parses, validates and reindexes brain/ markdown into the provenance index;
               brainkit.writer is the only sanctioned way to create a brain file
bridge/        the only package allowed to import both nvplan and brainkit (PLATFORM.md §7.1) -
               everything else stays decoupled. Joins the two directions: a decided decision
               drives a plan revenue override, and a decision can cite a real derivation as
               evidence. Also home to the *_check console scripts above.
brain/         the source of truth: plain markdown decisions, hypotheses, ingestion records and
               knowledge. Git-native and diffable; the database is a derived, rebuildable index.
data/          generated illustrative input data (`nvplan-gen-data`) and the hand-authored
               PDF used to build the original spec
scripts/       thin CLI wrappers kept for convenience (e.g. `scripts/demo.py` == `nvplan-demo`)
tests/         golden regression, consistency, gate, AI guardrails, API, demo and bridge tests
```

**Context management and skills**: the AI agents run behind deepagents middleware (large
tool-result eviction, token-triggered summarization, Anthropic prompt caching) and load one
`SKILL.md` per touchpoint from `nvplan/ai/skills/`. Every model call's literal request is logged to
`ai_record.call_log_json` and served by `GET /ai/records/{id}`. Details and tuning are in
`nvplan/ai/README.md`.

## Where to read next

- `PLATFORM.md` - the platform architecture contract: the provenance rule, the brain's layout and
  lifecycles, the bridge contract, and what's explicitly not built yet.
- `PLAN.md` - the finance module's own build plan and phase history.
- `UI.md` - the demo/testing interface contract (views, endpoints, what it is and isn't for).
- `VERIFICATION.md` - the hand-checked figures: what was independently verified against the
  illustrative data, and by what method.
- `nvplan/ai/README.md` - the AI layer in full: the three touchpoints, guardrails, running against
  the real model, and context-management tuning.
