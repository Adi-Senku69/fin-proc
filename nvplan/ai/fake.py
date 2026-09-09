"""Scripted fake chat model for offline tests of the AI layer.

``FakeMessagesListChatModel`` cannot be used directly with deepagents because
``create_agent`` calls ``bind_tools``; ``FakeToolCallingModel`` adds a no-op
``bind_tools`` (see the deepagents API notes). Responses are consumed in order
and cycle when exhausted, so every scripted run ends with the structured-output
tool call, which terminates the agent loop.

One scripted scenario per touchpoint keeps the tests deterministic. Every
script starts with a ``read_file`` of the touchpoint's skill
(``/skills/<name>/SKILL.md``), which proves the skills route of the composite
backend resolves; the tests assert the skill text came back in the ToolMessage.
``fake_summary_model`` is a separate instance for the summarization middleware
(a shared fake would consume scripted turns).
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

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


def scripted_deviation_model(
    *,
    scenario_kind: str,
    year: int,
    summary: str,
    contributions: list[dict[str, Any]],
) -> FakeToolCallingModel:
    """Reads plan vs actual once, then returns DeviationExplanation."""
    explanation = DeviationExplanation(scenario=scenario_kind, year=year, summary=summary, contributions=contributions)
    return FakeToolCallingModel(
        responses=[
            read_skill("deviation_explanation"),
            ai_calls(tool_call("get_plan_vs_actual", {"scenario_kind": scenario_kind, "year": year}, "pva")),
            structured("DeviationExplanation", explanation.model_dump(), content=summary),
        ]
    )
