# nvplan.ai — the advisory layer

Built on **deepagents 0.7.13** (`langchain` 1.4, `langchain-anthropic` 1.7; pinned in
`pyproject.toml` as `deepagents>=0.7.13`). Verified API: `create_deep_agent(model, tools, *,
system_prompt=..., response_format=..., subagents=[...], middleware=[...], backend=...)`.
`instructions=` no longer exists.

## The three touchpoints (PDF section "AI touchpoints")

| PDF touchpoint | entrypoint | reads | writes | structured result |
|---|---|---|---|---|
| 1. Environmental scan of the 54-position framework | `run_env_scan(session_factory, model=, positions_subset=)` | actuals, notes, framework, parameters | `external_note` rows (`source=ai_scan`, linked via `external_note.ai_record_id`) + one `ai_record` | `EnvScanResult` |
| 2. Revenue proposal with rationale, only on a flagged external factor | `run_revenue_proposal(session_factory, scenario_kind=, year=, default_value=, model=)` | revenue actuals, notes, control table, plan values, parameters | one `ai_record` (`touchpoint=revenue_proposal`, `status=proposed`, `proposed_value`) | `RevenueProposal` |
| 3. Deviation explanation citing the contributing figures | `run_deviation_explanation(session_factory, scenario_kind=, year=, model=)` | `get_plan_vs_actual` (pure arithmetic incl. alpha/beta split) | one `ai_record` | `DeviationExplanation` |

`build_advisor(model, session_factory)` wraps the three as deepagents subagents
(`env-scan`, `revenue-proposal`, `deviation-explanation`) behind one agent for free-form
questions.

## Hard rules and where they live

* **Read-only / advisory** — `tools.make_read_tools` only selects. `tools.make_write_tools`
  exposes exactly `record_external_note` and `record_revenue_proposal`; deepagents' built-in
  `write_file/edit_file/delete` are removed by a read-only `FilesystemMiddleware(tools=["read_file"])`.
  `tests/test_ai_guardrails.py` asserts row counts of `plan_value, statement_line, actual,
  parameter, derivation, scenario` are unchanged after every run and that no agent carries a
  tool named write*/insert*/update*/delete*/edit* besides the two allowed.
* **Rationale required** — `tools.validate_revenue_proposal` rejects an empty rationale (and a
  value outside `control_table.yaml: max_deviation_from_default_pct`, or a proposal that cites
  no existing note when `must_cite_note`). Rejection = error JSON to the model, nothing written.
* **Literal prompt stored** — `prompts.py` holds the three prompt templates as constants.
  `ai_record.prompt_text` = the system prompt exactly as the model received it (captured by a
  callback on the first model call) + `---USER---` + the rendered user prompt.
  `response_text` = the model's final prose + `---STRUCTURED---` + the JSON of the structured
  response. `model_version` = `ChatAnthropic.model` (or `"fake"` in tests).
* **Confirmation gate is elsewhere** — every record is written with `status=proposed`; this
  package never changes status (`services/gate.py` does).

Revenue-proposal persistence: the `record_revenue_proposal` tool inserts the `ai_record`
during the run (validation happens where the model can read the error and retry); the
entrypoint then finalises the same row with the prompt/response text. If the model skipped the
tool, the entrypoint pushes the structured proposal through the same validator and raises
`ProposalRejected`. One run produces at most one `ai_record`.

## Running with a real model

```bash
export ANTHROPIC_API_KEY=...
uv run python -c "
from nvplan.db.session import SessionLocal, get_engine, init_db
from nvplan.ai import run_revenue_proposal
init_db(get_engine('sqlite:///nvplan.db'))
rec = run_revenue_proposal(SessionLocal, scenario_kind='base', year=2027, default_value=23100.0)
print(rec.id, rec.proposed_value, rec.rationale)
"
```

`model=None` resolves to `ChatAnthropic(model=config.AI_MODEL)` (`claude-opus-5`); pass a model
name string or any `BaseChatModel` to override. Tests use `nvplan.ai.fake.FakeToolCallingModel`
(a `FakeMessagesListChatModel` with a no-op `bind_tools`) and one scripted scenario per
touchpoint, so they run without network or key.

Note: deepagents auto-adds a `general-purpose` subagent (`task` tool) to every agent; it
inherits the same tool set, so it cannot write anything the touchpoint itself cannot.
