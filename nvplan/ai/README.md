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
| 3. Deviation explanation citing the contributing figures | `run_deviation_explanation(session_factory, scenario_kind=, year=, model=)` | `get_plan_vs_actual` (pure arithmetic incl. alpha/beta split) | one `ai_record`, only after the numeric cross-check (see "Deviation cross-check") | `DeviationExplanation` |

`build_advisor(model, session_factory)` wraps the three as deepagents subagents
(`env-scan`, `revenue-proposal`, `deviation-explanation`) behind one agent for free-form
questions.

## Hard rules and where they live

* **Read-only / advisory** — `tools.make_read_tools` only selects. `tools.make_write_tools`
  exposes exactly `record_external_note` and `record_revenue_proposal`; deepagents' built-in
  `write_file/edit_file/delete` are removed by a read-only
  `FilesystemMiddleware(tools=["read_file", "ls", "grep"])` (see "Context management").
  `tests/test_ai_guardrails.py` asserts row counts of `plan_value, statement_line, actual,
  parameter, derivation, scenario` are unchanged after every run and that no agent carries a
  tool named write*/insert*/update*/delete*/edit* besides the two allowed.
* **Rationale required** — `tools.validate_revenue_proposal` rejects an empty rationale (and a
  value outside `control_table.yaml: max_deviation_from_default_pct`, or a proposal that cites
  no existing note when `must_cite_note`). Rejection = error JSON to the model, nothing written.
* **Literal prompt stored** — `prompts.py` holds the three prompt templates as constants.
  `ai_record.prompt_text` = the system prompt exactly as the model received it (captured by a
  callback on the first model call; it includes the skill index deepagents appends) +
  `---USER---` + the rendered user prompt. `response_text` = the model's final prose +
  `---STRUCTURED---` + the JSON of the structured response. `model_version` =
  `ChatAnthropic.model` (or `"fake"` in tests). Because the context middleware can rewrite
  what later calls see, `ai_record.call_log_json` additionally stores every model call of the
  run verbatim (see "Context management: the audit trail").
* **Deviation calculated deterministically, explained by AI** — `figures.plan_vs_actual` is the
  one code path behind both the `get_plan_vs_actual` tool and the cross-check
  `figures.check_explanation` that `run_deviation_explanation` applies to the structured
  result before writing anything: every category of the table exactly once, `plan`/`actual`/
  `deviation` within 0.05 k EUR of the deterministic value (i.e. equal to one decimal), and
  each contribution's `explanation` text quoting its deviation to one decimal (thousands
  separators and `+`/no sign tolerated, e.g. `-245.7`, `-1,245.7`, `-1 245.7`, `+32.9`/`32.9`).
  Any mismatch raises `ExplanationRejected` (a `ValueError`; HTTP 422) listing the offending
  fields with model vs deterministic values, and no `ai_record` exists. The rule is stated in
  the system prompt and in the deviation SKILL.md. `tests/test_ai_touchpoints.py` covers the
  wrong figure, the missing category, the prose without the figure, the tolerance edge and
  the 422.
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
touchpoint, so they run without network or key. The deviation script
(`fake.ScriptedDeviationModel`) fills its contributions from the deterministic table it was
shown (the `get_plan_vs_actual` tool result, or a `table=` handed in) unless a test scripts
them explicitly to provoke a rejection.

Note: deepagents auto-adds a `general-purpose` subagent (`task` tool) to every agent; it
inherits the same tool set, so it cannot write anything the touchpoint itself cannot.

## Skills (`nvplan/ai/skills/<name>/SKILL.md`)

The "how to work" procedures live in three deepagents skills, not in the system prompts:

| skill | used by | holds |
|---|---|---|
| `env-scan-54-positions` | env_scan | the 6x9 framework walk, the materiality rubric (low/medium/high), what counts as a source, one note per material position, always name domain/position ids |
| `revenue-proposal-method` | revenue_proposal | the arithmetic recipe with a worked example, the control-table rules, "propose the default if no note justifies a change", how to cite note ids |
| `deviation-explanation-method` | deviation_explanation | the beta-driven vs residual decomposition, numeric citation per category, the fixed-part assumption, no speculation beyond the figures |

Each system prompt keeps role, ground rules and units plus one line: "read your skill first:
`read_file("/skills/<name>/SKILL.md")`". Every agent is built with `skills=["/skills/"]`
(subagents carry the same `skills` key), so deepagents' `SkillsMiddleware` appends the skill
index ("## Skills System ... **env-scan-54-positions** ... -> Read `/skills/.../SKILL.md`") to
the system prompt; the agent then reads the file with the ordinary `read_file` tool. Frontmatter
rules: `name` must equal the directory name (lowercase, hyphens), `description` <= 1024 chars.
The fake scripts start with that `read_file` so the tests prove the path resolves
(`tests/test_ai_skills.py`).

Where the file comes from: the agents' backend is
`CompositeBackend(default=StateBackend(), routes={"/skills/": FilesystemBackend(root_dir=nvplan/ai/skills, virtual_mode=True)})`
(`context.make_backend`). `CompositeBackend` strips the route prefix, so `/skills/x/SKILL.md`
maps to `nvplan/ai/skills/x/SKILL.md` on disk; everything else (`/large_tool_results/`,
`/conversation_history/`) lives in graph state and vanishes with the run. The route works with
`SkillsMiddleware`'s `ls` + `download_files` unchanged - no seeding via `invoke(files=...)` needed.

## Context management (`nvplan/ai/context.py`, `nvplan/ai/audit.py`)

`ContextPolicy` (frozen dataclass; defaults from `config.AI_CONTEXT_*`) drives three middleware
that `build_context_middleware(policy, model=, backend=)` returns and every agent installs.
Compiled order (printed with `context.middleware_names(agent)`, asserted in
`tests/test_ai_context.py::test_middleware_order_audit_after_context`):

```
SkillsMiddleware > FilesystemMiddleware > SubAgentMiddleware > SummarizationMiddleware
  > ContextAuditMiddleware > AnthropicPromptCachingMiddleware
```

(first = outermost for `wrap_model_call`; `PatchToolCallsMiddleware` is `before_agent`-only.)

| what | fires when | default | tunable |
|---|---|---|---|
| **Tool-result eviction** (`FilesystemMiddleware`, at tool time) | a tool result longer than `tool_result_evict_tokens` x 4 chars | 4 000 tokens (the 54-position framework is ~1 500, the full actuals ~1 300, so nothing evicts by default) | `tool_result_evict_tokens` |
| **Summarization + offload** (deepagents `SummarizationMiddleware`, request-only) | approx. tokens of system + messages + tool schemas > `summarization_trigger_tokens` | 120 000; keeps the last 6 messages verbatim, older ones are summarized by `summarization_model` (default: the agent's model) and written in full to `/conversation_history/session_<id>.md` in state | `summarization_trigger_tokens`, `summarization_keep_messages`, `summarization_model` |
| **Prompt caching** (`AnthropicPromptCachingMiddleware`) | every call on a `ChatAnthropic` model; no-op otherwise | `ttl="5m"` | `cache_ttl` (`"5m"` or `"1h"`) |

Evicted results are replaced by a pointer + preview ("Tool result too large, the result of this
tool call `<id>` was saved in the filesystem at this path: `/large_tool_results/<id>` ... use
the read_file tool ... offset and limit"); the model pages through the file with
`read_file(offset, limit)` (filesystem tools themselves are never evicted; a `read_file` page is
capped at the same `tool_result_evict_tokens` x 4 chars). Line-based paging is why
`get_env_framework` is pretty-printed and `get_actuals` prints one row per line
(`tests/test_ai_context.py::test_evicted_actuals_are_readable_row_by_row`: evicted, then a
scripted `read_file(offset, limit)` returns the ten PERS rows incl. 2025). Token counts are
`count_tokens_approximately` (chars/4 + 3 per message), never a model call - fakes and
`claude-opus-5` (no profile in langchain-anthropic 1.7.1) both work; that is why the trigger is
`("tokens", N)` and not a fraction of the context window. `result["messages"]` always keeps
the raw history; only the request is rewritten.

**Eviction only, no clearing (decision).** `ContextEditingMiddleware(ClearToolUsesEdit)` was
tried and removed. Clearing replaces old tool results in the request with `[cleared]`; once a
data-bearing result (actuals, parameters, plan-vs-actual, notes) is blanked the model can only
cite those figures from memory, which is exactly what this layer must not do. Eviction keeps
the data retrievable: the big result moves to a file, the pointer stays in the conversation,
and the model reads it back with `read_file`. Summarization is kept because its offload is
also retrievable (`/conversation_history/`) and the summary names the file.
`tests/test_ai_context.py::test_no_tool_result_is_ever_cleared` pins this down.

Tuning: pass `policy=ContextPolicy(...)` to `run_*`, `build_touchpoint_agent` or
`build_advisor`; `None` means `DEFAULT_POLICY` (config). Give the summarizer a cheaper model
via `summarization_model=` if the runs get long.

### The audit trail

`ContextAuditMiddleware` (last in the list, so it sees the request after summarization)
appends one dict per model call to `AiRunContext.call_log`:

`call_index, timestamp, n_messages, approx_tokens, summarized, evicted_tool_results,
tools_offered, system_prompt_chars, request{system, messages[{role, content_excerpt (2 000
chars), tool_calls, tool_name, evicted, summary}]}, response{stop, text_excerpt, tool_calls,
usage}`.

`summarized` is detected by the summary `HumanMessage` deepagents inserts
(`additional_kwargs["lc_source"] == "summarization"`), with a fallback comparing the request's
first message to the state's first message; `evicted_tool_results` counts pointers to
`/large_tool_results/`. All three
entrypoints persist the log as `ai_record.call_log_json`; `GET /ai/records/{id}` returns it as
`call_log` (the list endpoint stays light). Together with `prompt_text` this is the "literal
prompt used at any AI step" - per step.
