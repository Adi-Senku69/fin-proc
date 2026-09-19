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
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration

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
