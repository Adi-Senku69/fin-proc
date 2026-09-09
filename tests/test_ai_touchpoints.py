"""The three AI touchpoints, run offline with the scripted fake model."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from nvplan.ai import ProposalRejected, run_deviation_explanation, run_env_scan, run_revenue_proposal
from nvplan.ai.fake import scripted_deviation_model, scripted_env_scan_model, scripted_revenue_model
from nvplan.ai.prompts import DEVIATION_EXPLANATION_SYSTEM, ENV_SCAN_SYSTEM, REVENUE_PROPOSAL_SYSTEM
from nvplan.db.models import Actual, AiRecord, AiStatus, Category, ExternalNote, NoteSource, Touchpoint
from test_ai_fixtures import PLAN_YEAR, ai_db  # noqa: F401 (fixture)

DEFAULT_2027 = 22_000.0


def _note_ids(factory) -> list[int]:
    with factory() as s:
        return list(s.scalars(select(ExternalNote.id).order_by(ExternalNote.id)).all())


# --------------------------------------------------------------------------- revenue proposal


def test_revenue_proposal_persists_one_proposed_record(ai_db):
    factory, _ = ai_db
    note_ids = _note_ids(factory)
    rationale = (
        "Note 1: the major client contract (~9% of revenue) ends Q2 2027, so about half a year is lost: "
        f"{DEFAULT_2027:,.0f} * (1 - 0.045) = 21 010 -> rounded to 21 000."
    )
    model = scripted_revenue_model(
        year=2027,
        proposed_value=21_000.0,
        rationale=rationale,
        cited_note_ids=[note_ids[0]],
        flagged_factors=["client contract ends Q2 2027"],
    )

    rec = run_revenue_proposal(factory, scenario_kind="base", year=2027, default_value=DEFAULT_2027, model=model)

    with factory() as s:
        records = s.scalars(select(AiRecord)).all()
        assert len(records) == 1
        r = records[0]
        assert r.id == rec.id
        assert r.touchpoint is Touchpoint.revenue_proposal
        assert r.status is AiStatus.proposed
        assert r.confirmed_by is None and r.confirmed_at is None
        assert r.proposed_value == 21_000.0
        assert r.year == 2027
        assert r.rationale.strip() == rationale
        assert r.model_version == "fake"
        # literal prompt: full system prompt + rendered user prompt incl. the default
        assert REVENUE_PROPOSAL_SYSTEM in r.prompt_text
        assert "---USER---" in r.prompt_text
        assert f"{DEFAULT_2027:,.1f}" in r.prompt_text
        assert "2027" in r.prompt_text
        # response: prose + structured JSON
        assert "---STRUCTURED---" in r.response_text
        assert '"proposed_value": 21000.0' in r.response_text
        rev = s.scalar(select(Category).where(Category.code == "REV"))
        assert r.category_id == rev.id
        assert r.scenario_id is not None
        # cited ids are real notes
        existing = set(note_ids)
        assert note_ids[0] in existing


def test_revenue_proposal_outside_bound_is_rejected(ai_db):
    factory, _ = ai_db
    note_ids = _note_ids(factory)
    too_high = DEFAULT_2027 * 1.30  # 30% > 25% allowed
    model = scripted_revenue_model(
        year=2027, proposed_value=too_high, rationale="unrealistically optimistic", cited_note_ids=[note_ids[1]]
    )
    with pytest.raises(ProposalRejected, match="25"):
        run_revenue_proposal(factory, scenario_kind="base", year=2027, default_value=DEFAULT_2027, model=model)
    with factory() as s:
        assert s.scalars(select(AiRecord)).all() == []


def test_revenue_proposal_tool_returns_error_string_outside_bound(ai_db):
    """The tool itself (what the model sees) answers with an error and writes nothing."""
    from nvplan.ai.tools import AiRunContext, make_write_tools

    factory, _ = ai_db
    ctx = AiRunContext(year=2027, default_value=DEFAULT_2027, model_version="fake", prompt_text="p")
    tool = {t.name: t for t in make_write_tools(factory, ctx)}["record_revenue_proposal"]
    out = tool.invoke(
        {"year": 2027, "proposed_value": DEFAULT_2027 * 0.7, "rationale": "cut", "cited_note_ids": _note_ids(factory)[:1]}
    )
    assert isinstance(out, str) and "error" in out and "25" in out
    assert ctx.ai_record_id is None
    with factory() as s:
        assert s.scalars(select(AiRecord)).all() == []


def test_revenue_proposal_empty_rationale_is_rejected(ai_db):
    from nvplan.ai.tools import AiRunContext, make_write_tools

    factory, _ = ai_db
    ctx = AiRunContext(year=2027, default_value=DEFAULT_2027, model_version="fake", prompt_text="p")
    tool = {t.name: t for t in make_write_tools(factory, ctx)}["record_revenue_proposal"]
    out = tool.invoke({"year": 2027, "proposed_value": 21_500.0, "rationale": "   ", "cited_note_ids": _note_ids(factory)[:1]})
    assert "error" in out and "rationale" in out
    with factory() as s:
        assert s.scalars(select(AiRecord)).all() == []

    model = scripted_revenue_model(year=2027, proposed_value=21_500.0, rationale="", cited_note_ids=_note_ids(factory)[:1])
    with pytest.raises(ProposalRejected, match="rationale"):
        run_revenue_proposal(factory, scenario_kind="base", year=2027, default_value=DEFAULT_2027, model=model)
    with factory() as s:
        assert s.scalars(select(AiRecord)).all() == []


def test_revenue_proposal_must_cite_existing_note(ai_db):
    factory, _ = ai_db
    model = scripted_revenue_model(year=2027, proposed_value=21_500.0, rationale="cites nothing real", cited_note_ids=[9999])
    with pytest.raises(ProposalRejected, match="9999"):
        run_revenue_proposal(factory, scenario_kind="base", year=2027, default_value=DEFAULT_2027, model=model)
    model = scripted_revenue_model(year=2027, proposed_value=21_500.0, rationale="cites nothing", cited_note_ids=[])
    with pytest.raises(ProposalRejected, match="cite"):
        run_revenue_proposal(factory, scenario_kind="base", year=2027, default_value=DEFAULT_2027, model=model)
    with factory() as s:
        assert s.scalars(select(AiRecord)).all() == []


# --------------------------------------------------------------------------- env scan


def test_env_scan_persists_record_and_linked_notes(ai_db):
    factory, _ = ai_db
    before = _note_ids(factory)

    rec = run_env_scan(factory, model=scripted_env_scan_model(), positions_subset=["D1", "D2.P4"])

    with factory() as s:
        r = s.get(AiRecord, rec.id)
        assert r.touchpoint is Touchpoint.env_scan
        assert r.status is AiStatus.proposed
        assert r.rationale.strip()
        assert ENV_SCAN_SYSTEM in r.prompt_text
        assert "D1.P2 Public-sector IT budgets" in r.prompt_text
        assert "D2.P4 Wage growth" in r.prompt_text
        assert "D2.P5" not in r.prompt_text  # subset respected
        assert "---STRUCTURED---" in r.response_text and '"materiality": "high"' in r.response_text

        new_notes = s.scalars(select(ExternalNote).where(ExternalNote.id.notin_(before))).all()
        assert len(new_notes) >= 1
        for n in new_notes:
            assert n.source is NoteSource.ai_scan
            assert n.ai_record_id == rec.id
            assert n.author == "ai:fake"
            assert n.text.strip()
        # the manual notes are untouched
        manual = s.scalars(select(ExternalNote).where(ExternalNote.id.in_(before))).all()
        assert all(n.source is NoteSource.manual and n.ai_record_id is None for n in manual)
        assert s.scalar(select(AiRecord.id).where(AiRecord.id != rec.id)) is None


# --------------------------------------------------------------------------- deviation explanation


def test_deviation_explanation_cites_figures(ai_db):
    factory, plans = ai_db
    with factory() as s:
        rev = s.scalar(select(Category).where(Category.code == "REV"))
        pers = s.scalar(select(Category).where(Category.code == "PERS"))
        actual_rev = s.scalar(select(Actual.value).where(Actual.category_id == rev.id, Actual.year == PLAN_YEAR))
        actual_pers = s.scalar(select(Actual.value).where(Actual.category_id == pers.id, Actual.year == PLAN_YEAR))
    plan_rev, plan_pers = plans["REV"], plans["PERS"]
    dev_rev, dev_pers = actual_rev - plan_rev, actual_pers - plan_pers

    summary = (
        f"Revenue came in at {actual_rev:.1f} vs plan {plan_rev:.1f} ({dev_rev:+.1f}); personnel {actual_pers:.1f} vs "
        f"{plan_pers:.1f} ({dev_pers:+.1f}), of which beta 0.1 * {dev_rev:+.1f} = {0.1 * dev_rev:+.1f} is revenue-driven."
    )
    model = scripted_deviation_model(
        scenario_kind="base",
        year=PLAN_YEAR,
        summary=summary,
        contributions=[
            {"category_code": "REV", "plan": plan_rev, "actual": actual_rev, "deviation": dev_rev, "explanation": "volume above plan"},
            {
                "category_code": "PERS",
                "plan": plan_pers,
                "actual": actual_pers,
                "deviation": dev_pers,
                "explanation": f"beta 0.1 * {dev_rev:+.1f} = {0.1 * dev_rev:+.1f}; residual {dev_pers - 0.1 * dev_rev:+.1f}",
            },
        ],
    )

    rec = run_deviation_explanation(factory, scenario_kind="base", year=PLAN_YEAR, model=model)

    with factory() as s:
        r = s.get(AiRecord, rec.id)
        assert r.touchpoint is Touchpoint.deviation_explanation
        assert r.status is AiStatus.proposed
        assert r.proposed_value is None
        assert r.year == PLAN_YEAR and r.scenario_id is not None
        assert DEVIATION_EXPLANATION_SYSTEM in r.prompt_text
        # the user prompt carried the arithmetic table
        assert f"plan {plan_rev:,.1f}, actual {actual_rev:,.1f}" in r.prompt_text
        # the stored response cites plan and actual figures
        assert f"{plan_rev:.1f}" in r.response_text
        assert f"{actual_rev:.1f}" in r.response_text
        assert f"{plan_pers:.1f}" in r.response_text
        assert f"{actual_pers:.1f}" in r.response_text
        assert r.rationale == summary
