"""Hard rules for the AI layer: read-only, rationale required, prompt stored, no status change."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from nvplan.ai import build_advisor, build_touchpoint_agent, run_deviation_explanation, run_env_scan, run_revenue_proposal
from nvplan.ai.agents import TOUCHPOINTS, touchpoint_tools
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
