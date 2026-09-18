# NewVision AI-Supported Planning PoC — Build Plan

Source of truth: `AI-Supported Planning — PoC Architecture & 3-Week Implementation Plan.pdf`
(PerfektWerk for Newvision Software GmbH, September 2026).

## 1. What the PDF asks for (condensed)

A planning engine for **five P&L categories** at total-company level where **every number is
traceable to its derivation in one click**. Transparency is the evaluation criterion.

| Category           | Kind    | Driver     | Notes                                              |
|--------------------|---------|------------|----------------------------------------------------|
| Revenue            | revenue | —          | Planned path (valorized default or AI-proposed)    |
| Material costs     | cost    | revenue    | Regression split                                   |
| External services  | cost    | revenue    | Regression split                                   |
| Personnel costs    | cost    | revenue    | ~80% of costs; most-interrogated fixed component   |
| Other costs        | cost    | revenue + investment | Depreciation from investment plan; remainder regressed |

**The mechanism (deterministic):**

```
Cost_c,t     = α_c + β_c · Revenue_t + ε          (OLS over historical window)
PlanCost_c,t = α_c · (1+v_c)^(t−t0) + β_c · PlanRevenue_t
```

- `α` fixed component, `β` variable rate, `R²` fit quality (surfaced), `v` valorization = avg YoY growth of the fixed part.
- Best / Base / Worst = three revenue paths through the same engine.
- P&L → Balance Sheet via a **config mapping** (illustrative until Newvision supplies theirs); Cash Flow = delta of BS movements.
- Deviation plan-vs-actual is arithmetic; backtest via MAPE / RMSE per category and horizon vs 5–8% threshold.

**AI is limited to three touchpoints, all advisory and read-only:**

1. Environmental scan (54-position framework) → writes `external_note`s.
2. Revenue proposal with written rationale, only when an external factor is flagged → `ai_record(status=proposed)`, human confirms before it enters `plan_value`.
3. Deviation explanation citing the contributing figures.

**Hard rules enforced in code:** no AI write access to source data; every AI value carries a rationale;
the literal prompt is stored and viewable; a `plan_value`/`statement_line` without a `derivation` cannot exist
(not-null FK).

**Nine tables:** category, actual, parameter, scenario, plan_value, external_note, ai_record, derivation, statement_line.

## 2. Decisions for this build

| Topic | Decision | Why |
|---|---|---|
| Language / tooling | Python 3.13, `uv`, `pytest` | User choice; uv already installed |
| Deterministic core | pandas + numpy, pure functions, no framework | Per PDF; exhaustively testable |
| Store | SQLite via SQLAlchemy 2.x | Per PDF |
| AI layer | **deepagents** (LangChain Deep Agents) with `langchain-anthropic`, model `claude-opus-5` | User choice; one orchestrating advisor agent with three subagents, one per touchpoint |
| AI in tests | Fake/scripted chat model, no network | No API key on this machine; core must not depend on AI |
| API | FastAPI | Per PDF |
| UI | Deferred. Trace output is exposed as a JSON lineage tree from the API and a CLI printer. React UI is a later phase. | User asked for Python first |
| Data | **Self-generated dummy data**, labelled `ILLUSTRATIVE` everywhere | Real figures not available |
| Front-loading | Deterministic core first, AI last | PDF week 1 → week 3 ordering; if AI is lost the engine still stands |

### 2.1 Model and provider decision (settled 2026-09-18)

The project stays on **Claude via `langchain-anthropic`**, model `claude-opus-5`. LiteLLM and other
gateway layers were considered and rejected for this build.

**Why not a gateway.** The provider seam is already one function, so a gateway buys no portability
we lack:

| Coupling point | What it is |
|---|---|
| `nvplan/ai/agents.py::get_model` | 3-line lazy constructor, the only place a real model is built |
| `nvplan/config.py::AI_MODEL` | the model id string |
| `nvplan/ai/context.py` | `AnthropicPromptCachingMiddleware`, self-disabling on other providers |

Everything else in the AI layer talks to LangChain's `BaseChatModel`. That is the same seam the
159 offline tests use to inject a fake model. Routing through a translation layer would cost:

- **Prompt caching.** The cache breakpoints on the system prompt, skills index and tool schemas
  silently no-op on a non-Anthropic model, so the stable prefix is re-billed at full input price
  on every call.
- **Adaptive thinking and effort control.** Anthropic-native request fields with no reliable
  equivalent through an OpenAI-shaped shim.
- **Prompt fidelity.** The audit middleware logs the request as LangChain builds it. A translation
  layer below that logging means the stored prompt is no longer byte-identical to what the provider
  received — a regression against the PDF's "the literal prompt is stored and viewable" rule.

**If hosting or data residency forces a change** (PDF open item 4, plausible for a German client),
take these in order, not a gateway:

1. Set the inference-geography parameter on the existing calls. Smallest change.
2. Run Claude on Bedrock (Frankfurt) or Vertex via the dedicated LangChain clients. Keeps caching
   and every Anthropic feature.
3. Only if a **non-Anthropic model is mandated**, or a company gateway is required for key
   management and spend control, introduce LiteLLM — as a **proxy**, never the Python wrapper. The
   AI layer leans hard on tool-call fidelity for skill reads, eviction pointers, proposal validation
   and structured output.

## 3. Dummy data design

Generated by `nvplan/data/generate.py`, seeded (`seed=42`), so the regression can be verified
against the parameters that produced the data (that is the golden test).

- Years **2016–2025** actuals (10 years; regression window defaults to last 5, 2021–2025). Units: k€.
- Revenue: starts 12,000 k€, grows 5–7%/yr with mild noise → ~20,800 k€ by 2025.
- Each cost category is produced as `α·(1+v)^(t−t0) + β·Revenue_t + noise`, with true parameters:

| Category | α (k€, 2016) | v | β | noise σ |
|---|---|---|---|---|
| Material costs | 300 | 2.0% | 0.045 | 1.5% |
| External services | 200 | 3.0% | 0.035 | 2.0% |
| Personnel costs | 6,000 | 2.8% | 0.300 | 1.0% |
| Other costs (excl. depreciation) | 500 | 2.5% | 0.025 | 2.0% |

  This keeps personnel ≈ 79% of total costs, matching the PDF's ~80%. (Phase 0 note: the first draft
  used larger non-personnel parameters, which capped personnel at ~76%; they were scaled down.)
  Realised revenue runs 12,000 → 20,766 k€ over 2016–2025; exact parameters and realised noise are in
  `data/illustrative/true_parameters.json`.
- Investment plan: capex per year 2016–2030 (dummy), straight-line depreciation over 5 years → Depreciation line.
- P&L → BS mapping config (YAML/JSON): illustrative, e.g. revenue → receivables (DSO 45 days), material → payables (DPO 30 days), capex → fixed assets, net income → equity, cash as the balancing item.
- External notes: 3–4 dummy notes (e.g. "major client contract ends Q2 2027").
- 54-position environmental framework: dummy structure of 6 domains × 9 positions (PESTEL-like), stored as config.
- Control table: rules for the revenue proposal (bounds, e.g. proposal must stay within ±25% of the valorized default; must cite a note).
- Outputs written to `data/illustrative/`: `actuals.csv`, `actuals.xlsx`, `investment_plan.csv`, `bs_mapping.yaml`, `external_notes.csv`, `env_framework.yaml`, `control_table.yaml`.

## 4. Package layout

```
NewVision/
├── PLAN.md
├── pyproject.toml                  (uv project: nvplan)
├── data/illustrative/              generated dummy inputs
├── nvplan/
│   ├── config.py                   paths, settings, model name
│   ├── data/generate.py            dummy data generator (CLI: `nvplan-gen-data`)
│   ├── db/models.py                9 SQLAlchemy tables (+ enums)
│   ├── db/session.py               engine/session factory, create_all
│   ├── ingest/actuals.py           CSV/XLSX → actual rows (source_label, illustrative flag)
│   ├── core/ledger.py              Derivation record builder (formula_text, inputs, parameters)
│   ├── core/regression.py          OLS α, β, R², valorization per category
│   ├── core/projector.py           5-year projection × scenario, cascade via β
│   ├── core/depreciation.py        investment plan → depreciation schedule
│   ├── core/statements.py          P&L → BS (mapping) → CF (delta)
│   ├── core/deviation.py           plan vs actual arithmetic
│   ├── core/backtest.py            train early years, predict known year, MAPE/RMSE
│   ├── services/planning.py        orchestrates core → DB with derivations (the only writer of numbers)
│   ├── services/trace.py           lineage tree for any plan_value / statement_line
│   ├── services/gate.py            confirm / reject ai_record → cascade on confirm
│   ├── ai/tools.py                 read-only LangChain tools over DB + one `record_proposal` tool
│   ├── ai/prompts.py               the three literal prompts (stored verbatim in ai_record)
│   ├── ai/agents.py                deepagents: advisor + 3 subagents
│   ├── ai/fake.py                  scripted fake model for tests
│   └── api/app.py                  FastAPI routes
├── tests/                          golden tests, statement consistency, gate, AI (fake model)
└── scripts/demo.py                 end-to-end run on dummy data, prints a trace
```

## 5. Phases and agent assignments

Each phase is delegated to coding sub-agents; the orchestrator (this session) reviews and integrates.

| Phase | Work | Done when |
|---|---|---|
| 0 Scaffold + data | uv project, SQLAlchemy models, dummy data generator, ingest | `uv run pytest` green on model constraints; data files exist and load |
| 1 Deterministic core | regression, projector, depreciation, ledger, golden tests | Regression recovers generating parameters (β within tolerance, R² > 0.95); projection matches hand computation |
| 2 Statements + scenarios | 3 scenarios, BS mapping, CF delta, planning service writes with derivations | BS balances every year, CF ties to BS movement, all three scenarios consistent |
| 3 Analysis | deviation, backtest, trace service | MAPE/RMSE per category and horizon reported; trace tree for any number in one call |
| 4 AI layer (deepagents) | three touchpoints, prompt persistence, confirmation gate | Proposal persisted with prompt; nothing enters plan unconfirmed; tests pass with fake model |
| 5 API + demo | FastAPI routes, `scripts/demo.py` | End-to-end demo runs on dummy data |
| 6 Hand verification | Orchestrator checks numbers by hand against generator parameters | Reviewed figures documented in `VERIFICATION.md` |
| 7 Context middleware + skills | deepagents middleware: tool-result eviction, clear old tool results, summarization with token trigger, prompt caching; per-call audit log persisted on `ai_record`; one SKILL.md per touchpoint | Summarization/eviction proven offline; every model call's literal request stored; skills listed in the stored prompt |
| 8 Guardrail middleware (deferred) | Per-touchpoint tool allowlist in `wrap_tool_call`, model/tool call limits | Deferred by user decision on 2026-09-09 |

## 6. Quality gates (from the PDF, kept)

- Reproduction: engine reproduces the generating parameters within stated tolerance.
- Formula integrity: no hard-coded results; changing an input recalculates downstream.
- Consistency: three scenarios reconcile, BS balances, CF ties to BS movement.
- Backtested accuracy: MAPE/RMSE per category & horizon, honest where threshold missed.
- Provenance: every displayed figure traceable; every AI value carries prompt, rationale, confirmer.
- Hand verification: independent check pass before calling anything done.

## 7. Open items (carried from PDF)

1. Real account-to-category mapping and real actuals.
2. Newvision's own P&L → BS mapping.
3. Scan depth of the 54-position framework.
4. Platform / hosting; React UI.
