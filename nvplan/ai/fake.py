"""Scripted fake chat model for offline tests of the AI layer.

``FakeMessagesListChatModel`` cannot be used directly with deepagents because
``create_agent`` calls ``bind_tools``; ``FakeToolCallingModel`` adds a no-op
``bind_tools`` (see the deepagents API notes). Responses are consumed in order
and cycle when exhausted, so every scripted run ends with the structured-output
tool call, which terminates the agent loop.

One scripted scenario per touchpoint keeps the tests deterministic. The deviation
script (``ScriptedDeviationModel``) fills its contributions from the deterministic
plan-vs-actual table (the ``get_plan_vs_actual`` result it was given, or a table
passed in) unless the test scripts them explicitly, so the numeric cross-check in
``run_deviation_explanation`` passes by construction and tests can break one figure
on purpose. Every
script starts with a ``read_file`` of the touchpoint's skill
(``/skills/<name>/SKILL.md``), which proves the skills route of the composite
backend resolves; the tests assert the skill text came back in the ToolMessage.
``fake_summary_model`` is a separate instance for the summarization middleware
(a shared fake would consume scripted turns). ``refusal`` / ``refusing_model`` script the
Anthropic policy-refusal shape (HTTP 200, ``stop_reason="refusal"`` + ``stop_details``) that
``nvplan.ai.agents`` turns into ``ModelRefused``.

Deterministic default provider (A1)
------------------------------------
The second half of this module, below the scripted-scenario helpers, is not test scaffolding:
``DeterministicChatModel`` is the offline provider ``nvplan.ai.agents.resolve_model`` builds by
default when no Anthropic credential is resolvable (see that function's docstring). Unlike the
scripted models above - which replay a fixed, hand-written script regardless of what the tools
actually return, so a test controls exactly what "the model" said - ``DeterministicChatModel``
is genuinely reactive: it reads whatever the read tools return for the *real* database of a run
and computes its tool calls / structured answer from that content, the same way the reference
pattern's ``DeterministicProvider`` narrates only the facts a prompt actually carries (see A1's
task notes). Its rules are deliberately simple and honest rather than clever: a revenue proposal
leaves the valorized default unchanged unless an existing external note states a quantified
factor for the target year; an environmental-scan finding requires an existing note whose words
actually overlap the position's own name; a deviation explanation reads its contributions
straight off the deterministic ``plan_vs_actual`` table (``contributions_from_table``, defined
above for the scripted deviation model and reused here verbatim - both are "cite exactly what
the tool returned"); the assistant cites the real plan-value rows a tool call returned and
invents no figure. This is what makes it "a real quality floor" rather than a stub that returns
empty: every schema-shaped field is really data-derived, so the guardrail and citation checks
(``figures.check_explanation``, ``assistant.verify_answer``, ``tools.validate_revenue_proposal``)
are genuinely exercised on every offline run, not bypassed by a canned answer built to satisfy
them.

Control flow, for every touchpoint the same shape: look at which tools have already been called
(from the message history deepagents replays on every turn) and which tool results are already
available; if a read is still missing, request it; once the reads are in, take the one action a
human analyst would (write a note / record a proposal) when the data warrants it; then answer
with the structured schema, computed from the same tool results, never invented. No LLM call, no
randomness, no network - the whole thing is a pure function of the conversation so far.
"""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from nvplan.ai.schemas import DeviationExplanation, EnvScanResult, RevenueProposal


class FakeToolCallingModel(FakeMessagesListChatModel):
    """Scripted chat model that accepts (and ignores) bound tools."""

    model_version: str = "fake"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "FakeToolCallingModel":  # noqa: ARG002
        return self


def tool_call(name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def ai_calls(*calls: dict[str, Any], content: str = "") -> AIMessage:
    return AIMessage(content=content, tool_calls=list(calls))


def structured(schema_name: str, payload: dict[str, Any], content: str = "") -> AIMessage:
    """The final turn: a tool call named after the response schema (ToolStrategy)."""
    return ai_calls(tool_call(schema_name, payload, f"structured-{schema_name}"), content=content)


SKILL_PATHS: dict[str, str] = {
    "env_scan": "/skills/env-scan-54-positions/SKILL.md",
    "revenue_proposal": "/skills/revenue-proposal-method/SKILL.md",
    "deviation_explanation": "/skills/deviation-explanation-method/SKILL.md",
}


def read_skill(touchpoint: str) -> AIMessage:
    """First scripted turn of every touchpoint: read its SKILL.md."""
    return ai_calls(tool_call("read_file", {"file_path": SKILL_PATHS[touchpoint]}, f"skill-{touchpoint}"))


def fake_summary_model(summary: str = "FAKE SUMMARY: earlier tool reads of the data (details offloaded).") -> FakeToolCallingModel:
    """A dedicated fake for ``ContextPolicy.summarization_model`` (plain text response)."""
    return FakeToolCallingModel(responses=[AIMessage(content=summary)])


# --------------------------------------------------------------------------- scripted scenarios


def scripted_env_scan_model(findings: list[dict[str, Any]] | None = None, *, summary: str | None = None) -> FakeToolCallingModel:
    """Reads the framework and the notes, records one note per finding, returns EnvScanResult.

    Each finding: {domain, position, text, category_code, year, materiality, reasoning, source}.
    """
    findings = findings or [
        {
            "domain": "D2 Economic",
            "position": "D2.P4 Wage growth",
            "text": "Collective wage rounds in the sector point to +4-5% p.a. for 2026-2027, above the valorized fixed-cost growth.",
            "category_code": "PERS",
            "year": 2026,
            "materiality": "high",
            "reasoning": "Personnel is ~80% of costs; a 1.5pp gap to the valorization rate moves the plan materially.",
            "source": "assumption/illustrative",
        },
        {
            "domain": "D1 Political",
            "position": "D1.P2 Public-sector IT budgets",
            "text": "Public-sector IT budgets are expected to grow ~3% p.a.; supports the framework agreement ramp-up from H2 2026.",
            "category_code": "REV",
            "year": 2026,
            "materiality": "medium",
            "reasoning": "The framework agreement (existing note) depends on budget availability.",
            "source": "assumption/illustrative",
        },
    ]
    note_calls = [
        tool_call(
            "record_external_note",
            {
                "text": f["text"],
                "domain": f["domain"],
                "position": f["position"],
                "category_code": f.get("category_code"),
                "year": f.get("year"),
            },
            f"note-{i}",
        )
        for i, f in enumerate(findings)
    ]
    result = EnvScanResult(
        flagged=[
            {
                "domain": f["domain"],
                "position": f["position"],
                "materiality": f.get("materiality", "medium"),
                "reasoning": f["reasoning"],
                "source": f.get("source", "assumption/illustrative"),
            }
            for f in findings
        ],
        summary=summary or f"{len(findings)} material positions flagged; the rest of the framework is immaterial for the horizon.",
    )
    return FakeToolCallingModel(
        responses=[
            read_skill("env_scan"),
            ai_calls(tool_call("get_env_framework", {}, "fw"), tool_call("get_external_notes", {}, "notes")),
            ai_calls(*note_calls),
            structured("EnvScanResult", result.model_dump(), content=result.summary),
        ]
    )


def scripted_revenue_model(
    *,
    year: int,
    proposed_value: float,
    rationale: str,
    cited_note_ids: list[int],
    flagged_factors: list[str] | None = None,
    prose: str | None = None,
) -> FakeToolCallingModel:
    """Reads actuals/notes/rules, records the proposal via the tool, returns RevenueProposal."""
    proposal = RevenueProposal(
        year=year,
        proposed_value=proposed_value,
        rationale=rationale,
        cited_note_ids=cited_note_ids,
        flagged_factors=flagged_factors or [],
    )
    return FakeToolCallingModel(
        responses=[
            read_skill("revenue_proposal"),
            ai_calls(
                tool_call("get_actuals", {"category_code": "REV"}, "act"),
                tool_call("get_external_notes", {}, "notes"),
                tool_call("get_control_table", {}, "rules"),
            ),
            ai_calls(
                tool_call(
                    "record_revenue_proposal",
                    {
                        "year": year,
                        "proposed_value": proposed_value,
                        "rationale": rationale,
                        "cited_note_ids": cited_note_ids,
                    },
                    "prop",
                )
            ),
            structured("RevenueProposal", proposal.model_dump(), content=prose or rationale),
        ]
    )


def contributions_from_table(table: dict[str, Any]) -> list[dict[str, Any]]:
    """Correct ``Contribution`` dicts for every row of a ``plan_vs_actual`` table.

    The figures are copied from the deterministic table (never typed by hand) and every
    explanation quotes its deviation to one decimal, so the result passes
    ``figures.check_explanation``; tests perturb a copy to provoke a rejection.
    """
    rev = next((r for r in table.get("rows", []) if r.get("kind") == "revenue"), None)
    out: list[dict[str, Any]] = []
    for r in table.get("rows", []):
        expl = f"{r['category_code']} actual {r['actual']:,.1f} vs plan {r['plan']:,.1f}, {r['deviation']:+,.1f}"
        if r.get("deviation_pct") is not None:
            expl += f" ({r['deviation_pct']:+.1f}%)"
        if "revenue_driven_part" in r and rev is not None:
            expl += (
                f"; beta {r['beta']:.4f} * revenue deviation {rev['deviation']:+,.1f} = "
                f"{r['revenue_driven_part']:+,.1f} revenue-driven; residual {r['residual']:+,.1f}"
            )
        out.append(
            {
                "category_code": r["category_code"],
                "plan": r["plan"],
                "actual": r["actual"],
                "deviation": r["deviation"],
                "explanation": expl,
            }
        )
    return out


def _table_from_messages(messages: list[Any]) -> dict[str, Any] | None:
    """The most recent ``get_plan_vs_actual`` ToolMessage in the request, parsed."""
    for m in reversed(messages):
        if getattr(m, "type", None) != "tool" or getattr(m, "name", None) != "get_plan_vs_actual":
            continue
        content = m.content if isinstance(m.content, str) else "".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in m.content
        )
        try:
            data = json.loads(content)
        except ValueError:
            continue
        if isinstance(data, dict) and "rows" in data:
            return data
    return None


class ScriptedDeviationModel(FakeToolCallingModel):
    """Scripted deviation model whose final turn sources its figures from the deterministic table.

    When the scripted ``DeviationExplanation`` has no contributions, they are filled at the
    structured turn from ``table`` (a ``plan_vs_actual`` result handed in by the test) or,
    failing that, from the ``get_plan_vs_actual`` ToolMessage in the request - i.e. the
    fake cites exactly what the tool returned, like a well-behaved model. Explicit
    contributions are passed through untouched (so tests can script a wrong figure).
    """

    table: dict[str, Any] | None = None

    def _generate(self, messages: list[Any], stop: Any = None, run_manager: Any = None, **kwargs: Any) -> Any:
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        msg = result.generations[0].message
        calls = list(getattr(msg, "tool_calls", None) or [])
        if len(calls) == 1 and calls[0]["name"] == "DeviationExplanation" and not calls[0]["args"].get("contributions"):
            table = self.table or _table_from_messages(messages)
            if table is not None:
                args = {**calls[0]["args"], "contributions": contributions_from_table(table)}
                filled = AIMessage(content=msg.content, tool_calls=[{**calls[0], "args": args}])
                result.generations[0] = ChatGeneration(message=filled)
        return result


def scripted_deviation_model(
    *,
    scenario_kind: str,
    year: int,
    summary: str,
    contributions: list[dict[str, Any]] | None = None,
    table: dict[str, Any] | None = None,
) -> ScriptedDeviationModel:
    """Reads plan vs actual once, then returns DeviationExplanation.

    ``contributions=None``/``[]``: filled from the deterministic table at the structured turn
    (see ``ScriptedDeviationModel``); pass ``table`` when the tool result may no longer be in
    the request (summarization). Explicit contributions are used verbatim.
    """
    explanation = DeviationExplanation(scenario=scenario_kind, year=year, summary=summary, contributions=contributions or [])
    return ScriptedDeviationModel(
        responses=[
            read_skill("deviation_explanation"),
            ai_calls(tool_call("get_plan_vs_actual", {"scenario_kind": scenario_kind, "year": year}, "pva")),
            structured("DeviationExplanation", explanation.model_dump(), content=summary),
        ],
        table=table,
    )


# --------------------------------------------------------------------------- conversational assistant (UI.md Part 3)


def scripted_assistant_model(
    *,
    segments: list[dict[str, Any]],
    proposal: dict[str, Any] | None = None,
    turns: list[AIMessage] | None = None,
) -> FakeToolCallingModel:
    """Scripted assistant run: optional tool-call turns (default: one ``get_plan_values``
    read, standing in for "the model looked something up"), then the final ``AssistantAnswer``
    structured turn. ``segments`` are plain dicts matching ``TextSegment`` / ``FigureSegment``
    / ``ClaimSegment`` - the test scripts exactly what a well- or badly-behaved model would
    return, and ``nvplan.ai.assistant.verify_answer`` is what is actually being tested."""
    payload = {"segments": segments, "proposal": proposal, "ai_record_id": -1, "usage": {}}
    pre = turns if turns is not None else [ai_calls(tool_call("get_plan_values", {"scenario_kind": "base"}, "pv"))]
    return FakeToolCallingModel(responses=[*pre, structured("AssistantAnswer", payload)])


# --------------------------------------------------------------------------- refusals


REFUSAL_CATEGORY = "reasoning_extraction"
REFUSAL_EXPLANATION = "This request was declined by a safety classifier (scripted, offline)."


def refusal(
    content: str = "",
    *,
    category: str | None = REFUSAL_CATEGORY,
    explanation: str | None = REFUSAL_EXPLANATION,
) -> AIMessage:
    """An AI message shaped like a real Anthropic policy refusal.

    Opus 5 returns HTTP 200 with ``stop_reason="refusal"`` and a ``stop_details`` object;
    langchain-anthropic 1.7.1 has no handling for it, which is why
    ``nvplan.ai.agents`` raises ``ModelRefused`` on this shape.
    """
    details: dict[str, Any] = {"type": "refusal"}
    if category is not None:
        details["category"] = category
    if explanation is not None:
        details["explanation"] = explanation
    return AIMessage(content=content, response_metadata={"stop_reason": "refusal", "stop_details": details})


def refusing_model(*, before: list[AIMessage] | None = None, **kwargs: Any) -> FakeToolCallingModel:
    """A fake whose (only, or final) turn is a refusal.

    ``before`` scripts turns that run first - e.g. a ``record_revenue_proposal`` call - so a test
    can prove the entrypoint deletes what a write tool already committed when the run is then
    refused.
    """
    return FakeToolCallingModel(responses=[*(before or []), refusal(**kwargs)])


# --------------------------------------------------------------------------- deterministic default provider (A1)
#
# See the module docstring's "Deterministic default provider" section for why this exists and
# what it deliberately does and does not do. Everything below is a pure function of the message
# history deepagents replays on every model call - no instance state, no randomness, no network.

# The response-schema tool name deepagents' ToolStrategy adds -> which touchpoint/surface this
# run is. Determined from the tool names bound at bind_tools() time, exactly the same name each
# scripted scenario above already calls at its own final turn (see ``structured``).
_SCHEMA_SURFACE: dict[str, str] = {
    "EnvScanResult": "env_scan",
    "RevenueProposal": "revenue_proposal",
    "DeviationExplanation": "deviation_explanation",
    "AssistantAnswer": "assistant",
}

_STOPWORDS = frozenset(
    {"the", "and", "for", "with", "over", "from", "their", "this", "that", "its", "affecting", "relative",
     "against", "planning", "horizon", "current", "core", "positions", "position", "framework"}
)


def _tool_name(t: Any) -> str | None:
    """The name of a bound tool/schema, whatever shape ``bind_tools`` was handed."""
    name = getattr(t, "name", None)
    if isinstance(name, str) and name:
        return name
    if isinstance(t, type):
        return t.__name__
    if isinstance(t, dict):
        if isinstance(t.get("name"), str):
            return t["name"]
        fn = t.get("function")
        if isinstance(fn, dict) and isinstance(fn.get("name"), str):
            return fn["name"]
    return None


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return str(content)


def _calls_made(messages: list[Any]) -> set[str]:
    """Every tool name the model itself has already called, anywhere in the history."""
    names: set[str] = set()
    for m in messages:
        for tc in getattr(m, "tool_calls", None) or []:
            names.add(tc["name"] if isinstance(tc, dict) else tc.name)
    return names


def _tool_results(messages: list[Any], name: str) -> list[str]:
    return [_content_text(m.content) for m in messages if getattr(m, "type", None) == "tool" and getattr(m, "name", None) == name]


def _last_tool_json(messages: list[Any], name: str) -> Any | None:
    results = _tool_results(messages, name)
    if not results:
        return None
    try:
        return json.loads(results[-1])
    except ValueError:
        return None


def _human_text(messages: list[Any]) -> str:
    """The first human turn - the rendered user prompt every touchpoint/the assistant sends
    (a correction turn is its own fresh ``agent.invoke`` with a single human message - see
    ``nvplan.ai.assistant.ask`` - so "first" is also "only" in every real call)."""
    for m in messages:
        if getattr(m, "type", None) == "human":
            return _content_text(m.content)
    return ""


def _keywords(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in _STOPWORDS}


# --------------------------------------------------------------------------- env scan


def _score_positions(framework: dict[str, Any], notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flag a framework position only when an existing external note both (a) affects one of the
    categories the position itself names as affected and (b) shares a substantive word with the
    position's own name - the same "only when the supplied context actually speaks to the
    question" discipline the reference pattern's own scan uses, so an unrelated note can never
    puff up the flagged count."""
    findings: list[dict[str, Any]] = []
    for dom in framework.get("domains", []):
        for pos in dom.get("positions", []):
            affects = set(pos.get("affects", []))
            pos_words = _keywords(pos.get("name", ""))
            best: dict[str, Any] | None = None
            best_hits: list[str] = []
            for note in notes:
                if note.get("category_code") and note["category_code"] not in affects:
                    continue
                hits = sorted(pos_words & _keywords(note.get("text", "")))
                if hits and len(hits) > len(best_hits):
                    best, best_hits = note, hits
            if best is None:
                continue
            category = best.get("category_code")
            findings.append(
                {
                    "domain": f"{dom['id']} {dom['name']}",
                    "position": f"{pos['id']} {pos.get('name', '')}",
                    "text": f"Existing note {best['id']} ({', '.join(best_hits[:3])}) bears on this position: {best['text']}",
                    "category_code": category,
                    "year": best.get("year"),
                    "materiality": "high" if category in ("REV", "PERS") else "medium",
                    "reasoning": (
                        f"External note {best['id']} shares {', '.join(best_hits[:3])} with this position's own "
                        f"name and affects {category or 'no specific category'}."
                    ),
                    "source": f"note:{best['id']}",
                }
            )
    return findings


def _env_scan_summary(findings: list[dict[str, Any]]) -> str:
    if not findings:
        return (
            "No existing external note shares enough of a framework position's own wording to flag it - "
            "nothing material found from the data on hand."
        )
    return f"{len(findings)} position(s) flagged from keyword overlap with existing external notes; see reasoning per finding."


# --------------------------------------------------------------------------- revenue proposal

_TARGET_YEAR_RE = re.compile(r"Target year:\s*(\d{4})")
_DEFAULT_VALUE_RE = re.compile(r"Valorized default revenue for \d{4}:\s*([\d, .]+?)\s*k EUR")
_PCT_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*%")
_REDUCE_WORDS = ("end", "loss", "lose", "lost", "declin", "drop", "reduc", "cancel", "churn", "delay", "slip")


def _target_year_and_default(messages: list[Any]) -> tuple[int, float]:
    text = _human_text(messages)
    year_m = _TARGET_YEAR_RE.search(text)
    default_m = _DEFAULT_VALUE_RE.search(text)
    year = int(year_m.group(1)) if year_m else 0
    default_value = float(default_m.group(1).replace(",", "").replace(" ", "")) if default_m else 0.0
    return year, default_value


def _propose_revenue(
    notes: list[dict[str, Any]], year: int, default_value: float, rules: dict[str, Any]
) -> tuple[float, str, list[int], list[str]]:
    """The reference pattern's own ``_propose``, adapted to real note rows instead of a raw
    prompt substring: read a magnitude off the note that actually names this year, adjust the
    valorized default by it (clamped to the control table's own bound so the write tool never
    rejects it), and say every step - or leave the default alone and say why, honestly, when
    nothing quantifies a change. ``must_cite_note`` is satisfied even in the "unchanged" case by
    citing whatever note exists, explicitly labelled as not driving the figure."""
    must_cite = bool(rules.get("must_cite_note"))
    max_pct = rules.get("max_deviation_from_default_pct")
    candidates = [n for n in notes if n.get("category_code") == "REV"]
    note = next((n for n in candidates if n.get("year") == year), candidates[0] if candidates else None)

    if note is None:
        cited = [int(notes[0]["id"])] if must_cite and notes else []
        rationale = (
            f"No external note flags a revenue-relevant factor for {year}, so the valorized default of "
            f"{default_value:,.1f} k EUR stands unchanged."
        )
        if cited:
            rationale += f" Note {cited[0]} is cited only to satisfy the citation rule; it does not drive this figure."
        return default_value, rationale, cited, []

    pct_match = _PCT_RE.search(note.get("text", ""))
    if not pct_match:
        rationale = (
            f"Note {note['id']} flags a revenue-relevant factor for {year} but states no quantified magnitude, "
            f"so the valorized default of {default_value:,.1f} k EUR stands unchanged."
        )
        return default_value, rationale, [int(note["id"])], [note.get("text", "")[:80]]

    magnitude = abs(float(pct_match.group(1))) / 100.0
    if max_pct is not None:
        magnitude = min(magnitude, max(float(max_pct) / 100.0 - 1e-6, 0.0))
    direction = -1.0 if any(w in note["text"].lower() for w in _REDUCE_WORDS) else 1.0
    proposed = default_value * (1.0 + direction * magnitude)
    rationale = (
        f"Note {note['id']} states a {magnitude:.0%} {'reduction' if direction < 0 else 'increase'} relevant to "
        f"{year}. Applied to the valorized default of {default_value:,.1f} k EUR: "
        f"{default_value:,.1f} * (1 {'-' if direction < 0 else '+'} {magnitude:.0%}) = {proposed:,.1f} k EUR."
    )
    return proposed, rationale, [int(note["id"])], [note.get("text", "")[:80]]


# --------------------------------------------------------------------------- deviation explanation


def _deviation_summary(table: dict[str, Any]) -> str:
    totals = table.get("totals", {})
    if totals.get("result_plan") is None:
        return f"Plan vs actual for {table.get('scenario')} {table.get('year')}, read directly from the deterministic table."
    return (
        f"{table.get('scenario')} {table.get('year')}: result moved from {totals['result_plan']:,.1f} "
        f"(plan) to {totals['result_actual']:,.1f} (actual); see each category's contribution below."
    )


# --------------------------------------------------------------------------- assistant

_SCENARIO_RE = re.compile(r"relevant \(best/base/worst\):\s*(\w+)")


def _scenario_kind_from_question(messages: list[Any]) -> str:
    m = _SCENARIO_RE.search(_human_text(messages))
    kind = (m.group(1) if m else "base").lower()
    return kind if kind in ("best", "base", "worst") else "base"


class DeterministicChatModel(BaseChatModel):
    """The offline default provider (see module doc). Reactive, not scripted: every tool call
    and every structured answer is computed from the real tool results in the conversation so
    far, never from a fixed script - see ``nvplan.ai.agents.resolve_model``."""

    model_version: str = "deterministic-v1"
    bound_tools: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:  # pragma: no cover - trivial
        return "nvplan-deterministic"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "DeterministicChatModel":  # noqa: ARG002
        self.bound_tools = [n for n in (_tool_name(t) for t in tools) if n]
        return self

    def _generate(self, messages: list[Any], stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:  # noqa: ARG002
        surface = next((s for schema, s in _SCHEMA_SURFACE.items() if schema in self.bound_tools), None)
        step = {
            "env_scan": self._env_scan_step,
            "revenue_proposal": self._revenue_proposal_step,
            "deviation_explanation": self._deviation_step,
            "assistant": self._assistant_step,
        }.get(surface, self._fallback_step)
        return ChatResult(generations=[ChatGeneration(message=step(messages))])

    # -- env scan --------------------------------------------------------------

    def _env_scan_step(self, messages: list[Any]) -> AIMessage:
        calls = _calls_made(messages)
        if "get_env_framework" not in calls:
            return ai_calls(tool_call("get_env_framework", {}, "det-fw"), tool_call("get_external_notes", {}, "det-notes"))
        framework = _last_tool_json(messages, "get_env_framework") or {}
        notes = (_last_tool_json(messages, "get_external_notes") or {}).get("rows", [])
        findings = _score_positions(framework, notes)
        if findings and "record_external_note" not in calls:
            note_calls = [
                tool_call(
                    "record_external_note",
                    {
                        "text": f["text"],
                        "domain": f["domain"],
                        "position": f["position"],
                        "category_code": f.get("category_code"),
                        "year": f.get("year"),
                    },
                    f"det-note-{i}",
                )
                for i, f in enumerate(findings)
            ]
            return ai_calls(*note_calls)
        result = EnvScanResult(
            flagged=[
                {"domain": f["domain"], "position": f["position"], "materiality": f["materiality"],
                 "reasoning": f["reasoning"], "source": f["source"]}
                for f in findings
            ],
            summary=_env_scan_summary(findings),
        )
        return structured("EnvScanResult", result.model_dump(), content=result.summary)

    # -- revenue proposal --------------------------------------------------------

    def _revenue_proposal_step(self, messages: list[Any]) -> AIMessage:
        calls = _calls_made(messages)
        needed_reads = {"get_actuals", "get_external_notes", "get_control_table"}
        if not needed_reads <= calls:
            return ai_calls(
                tool_call("get_actuals", {"category_code": "REV"}, "det-act"),
                tool_call("get_external_notes", {}, "det-notes"),
                tool_call("get_control_table", {}, "det-rules"),
            )
        notes = (_last_tool_json(messages, "get_external_notes") or {}).get("rows", [])
        rules = (_last_tool_json(messages, "get_control_table") or {}).get("revenue_proposal", {})
        year, default_value = _target_year_and_default(messages)
        proposed_value, rationale, cited_ids, factors = _propose_revenue(notes, year, default_value, rules)
        if "record_revenue_proposal" not in calls:
            return ai_calls(
                tool_call(
                    "record_revenue_proposal",
                    {"year": year, "proposed_value": proposed_value, "rationale": rationale, "cited_note_ids": cited_ids},
                    "det-prop",
                )
            )
        result = RevenueProposal(
            year=year, proposed_value=proposed_value, rationale=rationale, cited_note_ids=cited_ids, flagged_factors=factors
        )
        return structured("RevenueProposal", result.model_dump(), content=rationale)

    # -- deviation explanation ----------------------------------------------------

    def _deviation_step(self, messages: list[Any]) -> AIMessage:
        calls = _calls_made(messages)
        table = _last_tool_json(messages, "get_plan_vs_actual")
        if "get_plan_vs_actual" not in calls or table is None:
            # scenario_kind/year are not yet known from a tool result; read them off the rendered
            # user prompt (the same figures the entrypoint used to build it) for this one call.
            m = re.search(r"Scenario:\s*(\w+)\.\s*Year:\s*(\d{4})", _human_text(messages))
            scenario_kind, year = (m.group(1), int(m.group(2))) if m else ("base", 0)
            return ai_calls(tool_call("get_plan_vs_actual", {"scenario_kind": scenario_kind, "year": year}, "det-pva"))
        contributions = contributions_from_table(table)
        summary = _deviation_summary(table)
        result = DeviationExplanation(
            scenario=table.get("scenario", "base"), year=table.get("year", 0), summary=summary, contributions=contributions
        )
        return structured("DeviationExplanation", result.model_dump(), content=summary)

    # -- assistant ------------------------------------------------------------

    def _assistant_step(self, messages: list[Any]) -> AIMessage:
        calls = _calls_made(messages)
        if "get_plan_values" not in calls:
            scenario_kind = _scenario_kind_from_question(messages)
            return ai_calls(tool_call("get_plan_values", {"scenario_kind": scenario_kind}, "det-pv"))
        data = _last_tool_json(messages, "get_plan_values") or {}
        rows = data.get("rows", [])
        scenario = data.get("scenario") or _scenario_kind_from_question(messages)
        if not rows:
            segments: list[dict[str, Any]] = [
                {"type": "text", "text": f"No plan values are available yet for the {scenario} scenario."}
            ]
        else:
            segments = [
                {"type": "text", "text": f"Plan values for the {scenario} scenario, read directly from the plan grid."}
            ]
            segments.extend(
                {
                    "type": "figure",
                    "label": f"{r['category_code']} {r['year']}",
                    "value": r["value"],
                    "unit": "k EUR",
                    "ref": {"kind": "plan_value", "id": r["plan_value_id"]},
                }
                for r in rows
            )
        payload = {"segments": segments, "proposal": None, "ai_record_id": -1, "usage": {}}
        return structured("AssistantAnswer", payload)

    # -- anything else (e.g. the free-form advisor, which has no response schema of its own) ----

    def _fallback_step(self, messages: list[Any]) -> AIMessage:  # noqa: ARG002
        return AIMessage(
            content=(
                "This is the deterministic offline default provider: ask one of the three formal touchpoints "
                "(env-scan, revenue-proposal, deviation-explanation) or the assistant's question view for a "
                "grounded, data-derived answer."
            )
        )


def deterministic_model() -> DeterministicChatModel:
    """A fresh instance of the default, credential-free provider. Statelessness lives in the
    conversation, not the instance (see the class doc), so one instance is safe to share across
    a run's subagents; a fresh one per call is still cheapest and leaves no doubt."""
    return DeterministicChatModel()
