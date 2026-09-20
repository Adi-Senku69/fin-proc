"""Hard rules for the AI layer: read-only, rationale required, prompt stored, no status change."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from nvplan.ai import build_advisor, build_touchpoint_agent, run_deviation_explanation, run_env_scan, run_revenue_proposal
from nvplan.ai.agents import TOUCHPOINTS, touchpoint_tools
from nvplan.ai.evals.fixtures import new_platform_db
from nvplan.ai.fake import FakeToolCallingModel, scripted_deviation_model, scripted_env_scan_model, scripted_revenue_model
from nvplan.ai.tools import ALLOWED_WRITE_TOOLS, AiRunContext, make_read_tools, make_write_tools
from nvplan.db.models import AiRecord, AiStatus, ExternalNote
from test_ai_fixtures import PLAN_YEAR, ai_db, forbidden_counts  # noqa: F401 (fixture)

FORBIDDEN_PREFIXES = ("write", "insert", "update", "delete", "edit")


def _first_note_id(factory) -> int:
    with factory() as s:
        return s.scalar(select(ExternalNote.id).order_by(ExternalNote.id))


def _agent_tool_names(agent) -> set[str]:
    return set(agent.nodes["tools"].bound.tools_by_name)


def test_forbidden_tables_unchanged_after_every_run(ai_db):
    factory, plans = ai_db
    before = forbidden_counts(factory)

    run_env_scan(factory, model=scripted_env_scan_model(), positions_subset=["D2"])
    assert forbidden_counts(factory) == before

    run_revenue_proposal(
        factory,
        scenario_kind="base",
        year=2027,
        default_value=22_000.0,
        model=scripted_revenue_model(
            year=2027, proposed_value=21_000.0, rationale="contract ends Q2 2027", cited_note_ids=[_first_note_id(factory)]
        ),
    )
    assert forbidden_counts(factory) == before

    run_deviation_explanation(
        factory,
        scenario_kind="base",
        year=PLAN_YEAR,
        model=scripted_deviation_model(scenario_kind="base", year=PLAN_YEAR, summary="s", contributions=[]),
    )
    assert forbidden_counts(factory) == before

    # the only AI writes: ai_record rows (all proposed) and ai_scan notes
    with factory() as s:
        records = s.scalars(select(AiRecord)).all()
        assert len(records) == 3
        assert all(r.status is AiStatus.proposed for r in records)
        assert all(r.prompt_text and r.response_text and r.rationale.strip() for r in records)


def test_no_agent_carries_a_writing_tool(ai_db):
    factory, _ = ai_db
    ctx = AiRunContext(year=2027, default_value=22_000.0)
    fake = FakeToolCallingModel(responses=[])

    for tp in TOUCHPOINTS:
        names = {t.name for t in touchpoint_tools(tp, factory, ctx)}
        agent = build_touchpoint_agent(tp, model=fake, session_factory=factory, extra_context=ctx)
        all_names = _agent_tool_names(agent)
        assert names <= all_names
        for n in all_names:
            if n in ALLOWED_WRITE_TOOLS:
                continue
            assert not n.lower().startswith(FORBIDDEN_PREFIXES), f"{tp} exposes writing tool {n}"
        # deepagents' built-in filesystem tools are cut down to read_file
        assert "write_file" not in all_names and "edit_file" not in all_names and "delete" not in all_names

    advisor = build_advisor(fake, factory)
    for n in _agent_tool_names(advisor):
        assert n in ALLOWED_WRITE_TOOLS or not n.lower().startswith(FORBIDDEN_PREFIXES)

    read_names = {t.name for t in make_read_tools(factory)}
    assert all(n.startswith("get_") for n in read_names)
    assert {t.name for t in make_write_tools(factory, ctx)} == set(ALLOWED_WRITE_TOOLS)


def test_touchpoints_get_only_their_tools(ai_db):
    factory, _ = ai_db
    ctx = AiRunContext()
    env = {t.name for t in touchpoint_tools("env_scan", factory, ctx)}
    rev = {t.name for t in touchpoint_tools("revenue_proposal", factory, ctx)}
    dev = {t.name for t in touchpoint_tools("deviation_explanation", factory, ctx)}
    assert "record_external_note" in env and "record_revenue_proposal" not in env
    assert "record_revenue_proposal" in rev and "record_external_note" not in rev
    assert not (dev & ALLOWED_WRITE_TOOLS)
    assert "get_plan_vs_actual" in dev
    with pytest.raises(ValueError):
        touchpoint_tools("nope", factory, ctx)


def test_advisor_lists_three_touchpoint_subagents(ai_db):
    factory, _ = ai_db
    advisor = build_advisor(FakeToolCallingModel(responses=[]), factory)
    task = advisor.nodes["tools"].bound.tools_by_name["task"]
    for name in ("env-scan", "revenue-proposal", "deviation-explanation"):
        assert f"- {name}:" in task.description


def test_status_stays_proposed_and_nothing_confirms(ai_db):
    factory, _ = ai_db
    run_revenue_proposal(
        factory,
        scenario_kind="base",
        year=2027,
        default_value=22_000.0,
        model=scripted_revenue_model(year=2027, proposed_value=22_000.0, rationale="keep default", cited_note_ids=[_first_note_id(factory)]),
    )
    with factory() as s:
        r = s.scalar(select(AiRecord))
        assert r.status is AiStatus.proposed
        assert r.confirmed_by is None
        assert r.confirmed_at is None


# --------------------------------------------------------------------------- B3: status is never the model's to choose
#
# PLATFORM.md §12.3.4/§12.6: brainkit.writer's draft_decision has no status parameter, and a
# hypothesis item's status key (if present) is never read - B1's own guarantee. The two tests
# below check the SAME property one layer up, at the tool boundary draft_decision/draft_hypotheses
# actually expose to a model: not "brainkit enforces this if reached correctly" but "there is no
# argument on the tool call itself through which a model - compromised or not - could make this
# happen", which is what nvplan.ai.assistant / a real model call actually sees.

_DECISION_DRAFT_ARGS: dict = dict(
    slug="sunset-legacy-importer",
    title="Sunset the legacy CSV importer",
    date="2026-09-20",
    context="The legacy importer duplicates the new ingestion pipeline.",
    options=["Keep both importers", "Sunset the legacy importer"],
    decision="Sunset the legacy importer.",
    why="The new pipeline has fully replaced it.",
    evidence=[["The new pipeline has handled all import volume for two quarters.", "(industry-knowledge)"]],
    reversal="If the new pipeline's error rate exceeds 1% for two consecutive weeks.",
)


def _draft_write_tools(tmp_path):
    factory, _plans = new_platform_db(tmp_path / "db.sqlite")
    tools = {t.name: t for t in make_write_tools(factory, AiRunContext(), brain_root=tmp_path / "brain")}
    return tools


def test_draft_decision_tool_schema_has_no_status_argument(tmp_path):
    """The tool's own args schema names no ``status`` field at all - there is nowhere on the
    call for a model to put one, regardless of what a hostile prompt asks it to try."""
    tools = _draft_write_tools(tmp_path)
    assert "status" not in tools["draft_decision"].args_schema.model_fields


def test_draft_decision_tool_ignores_an_extra_status_argument_and_still_writes_pending(tmp_path):
    """The worst case: a compromised model calls the tool with an extra ``status="decided"``
    argument the schema does not declare anyway. LangChain's own arg-binding drops the
    unrecognized key before the function ever runs, so the draft proceeds exactly as if it had
    never been sent - and it still lands at ``pending``, never anything else."""
    tools = _draft_write_tools(tmp_path)
    out = tools["draft_decision"].invoke({**_DECISION_DRAFT_ARGS, "status": "decided"})
    assert '"status": "pending"' in out
    assert '"status": "decided"' not in out


def test_draft_hypotheses_item_rejects_an_extra_status_argument_outright(tmp_path):
    """Stronger than ignoring it: a hypothesis item is a closed pydantic model
    (``nvplan.ai.tools.HypothesisItemArg``, ``extra="forbid"``), so a status key on any item
    raises a validation error before this module's own code runs at all - nothing is written,
    not even at the correct status."""
    tools = _draft_write_tools(tmp_path)
    hostile_item = {"risk": "value", "belief": "Users want a faster import.", "status": "supported"}
    with pytest.raises(Exception, match="status"):
        tools["draft_hypotheses"].invoke({"feature_slug": "fast-import", "title": "Fast import", "hypotheses": [hostile_item]})
