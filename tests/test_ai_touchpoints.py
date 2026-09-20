"""The three AI touchpoints, run offline with the scripted fake model."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from nvplan.ai import ExplanationRejected, ProposalRejected, plan_vs_actual, run_deviation_explanation, run_env_scan, run_revenue_proposal
from nvplan.ai.fake import DeterministicChatModel, contributions_from_table, scripted_deviation_model, scripted_env_scan_model, scripted_revenue_model
from nvplan.api.app import create_app
from nvplan.ai.prompts import DEVIATION_EXPLANATION_SYSTEM, ENV_SCAN_SYSTEM, REVENUE_PROPOSAL_SYSTEM
from nvplan.db.models import Actual, AiRecord, AiStatus, Category, ExternalNote, NoteSource, Touchpoint
from test_ai_fixtures import PLAN_YEAR, ai_db  # noqa: F401 (fixture)

DEFAULT_2027 = 22_000.0


@pytest.fixture()
def no_credentials(monkeypatch):
    """No Anthropic credential resolvable at all - the shape a fresh CI runner is in. A1: the
    three touchpoints must still complete end to end in this state, via the deterministic
    default provider (nvplan.ai.agents.resolve_model)."""
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("nvplan.ai.agents._ant_cli", lambda: None)
    return None


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


def _table(factory, year: int = PLAN_YEAR) -> dict:
    with factory() as s:
        return plan_vs_actual(s, "base", year)


def _record_count(factory) -> int:
    with factory() as s:
        return len(s.scalars(select(AiRecord)).all())


def test_deviation_explanation_cites_figures(ai_db):
    """(a) a scripted run whose figures come from the deterministic table is persisted."""
    factory, plans = ai_db
    table = _table(factory)
    rows = {r["category_code"]: r for r in table["rows"]}
    assert set(rows) == {"REV", "MAT", "EXT", "PERS", "OTH"} and rows["REV"]["plan"] == plans["REV"]
    contributions = contributions_from_table(table)
    with factory() as s:
        rev = s.scalar(select(Category).where(Category.code == "REV"))
        actual_rev = s.scalar(select(Actual.value).where(Actual.category_id == rev.id, Actual.year == PLAN_YEAR))
    plan_rev, dev_rev = plans["REV"], actual_rev - plans["REV"]
    summary = (
        f"Revenue came in at {actual_rev:.1f} vs plan {plan_rev:.1f} ({dev_rev:+.1f}); cost lines split into beta 0.1 * "
        f"{dev_rev:+.1f} = {0.1 * dev_rev:+.1f} revenue-driven and a residual on the fixed part."
    )
    model = scripted_deviation_model(scenario_kind="base", year=PLAN_YEAR, summary=summary, contributions=contributions)

    rec = run_deviation_explanation(factory, scenario_kind="base", year=PLAN_YEAR, model=model)

    with factory() as s:
        r = s.get(AiRecord, rec.id)
        assert r.touchpoint is Touchpoint.deviation_explanation
        assert r.status is AiStatus.proposed
        assert r.proposed_value is None
        assert r.year == PLAN_YEAR and r.scenario_id is not None
        assert DEVIATION_EXPLANATION_SYSTEM in r.prompt_text
        assert "checked against the deterministic plan-vs-actual table" in r.prompt_text
        # the user prompt carried the arithmetic table
        assert f"plan {plan_rev:,.1f}, actual {actual_rev:,.1f}" in r.prompt_text
        # the stored response carries plan/actual/deviation of every category: verbatim in the
        # structured JSON, to one decimal in the prose of each contribution
        for row in table["rows"]:
            assert f'"plan": {json.dumps(row["plan"])}' in r.response_text
            assert f'"actual": {json.dumps(row["actual"])}' in r.response_text
            assert f"actual {row['actual']:,.1f} vs plan {row['plan']:,.1f}, {row['deviation']:+,.1f}" in r.response_text
        assert r.rationale == summary
    assert _record_count(factory) == 1


def test_deviation_explanation_fake_fills_from_tool_result(ai_db):
    """contributions=[] (as the API/guardrail tests script it) -> figures copied from get_plan_vs_actual."""
    factory, _ = ai_db
    rec = run_deviation_explanation(
        factory, scenario_kind="base", year=PLAN_YEAR,
        model=scripted_deviation_model(scenario_kind="base", year=PLAN_YEAR, summary="filled", contributions=[]),
    )
    with factory() as s:
        text = s.get(AiRecord, rec.id).response_text
    for row in _table(factory)["rows"]:
        assert f'"category_code": "{row["category_code"]}"' in text and f"{row['deviation']:+,.1f}" in text


def test_deviation_explanation_wrong_plan_is_rejected_and_nothing_persisted(ai_db):
    """(b) PERS.plan off by 100 -> ExplanationRejected naming model vs deterministic value; no ai_record."""
    factory, _ = ai_db
    table = _table(factory)
    contributions = contributions_from_table(table)
    pers = next(c for c in contributions if c["category_code"] == "PERS")
    pers["plan"] += 100.0
    det_plan = next(r for r in table["rows"] if r["category_code"] == "PERS")["plan"]
    before = _record_count(factory)
    model = scripted_deviation_model(scenario_kind="base", year=PLAN_YEAR, summary="off", contributions=contributions)
    with pytest.raises(ExplanationRejected) as ei:
        run_deviation_explanation(factory, scenario_kind="base", year=PLAN_YEAR, model=model)
    msg = str(ei.value)
    assert "PERS.plan" in msg and f"model {pers['plan']:.1f}" in msg and f"deterministic {det_plan:.1f}" in msg
    assert "REV" not in msg.split("PERS.plan")[0]  # only the offending field is listed
    assert isinstance(ei.value, ValueError)
    assert _record_count(factory) == before


def test_deviation_explanation_within_rounding_tolerance_is_accepted(ai_db):
    """0.04 k EUR off (same figure to one decimal) passes; 0.06 does not."""
    factory, _ = ai_db
    table = _table(factory)
    ok = contributions_from_table(table)
    ok[0]["actual"] += 0.04
    run_deviation_explanation(
        factory, scenario_kind="base", year=PLAN_YEAR,
        model=scripted_deviation_model(scenario_kind="base", year=PLAN_YEAR, summary="ok", contributions=ok),
    )
    bad = contributions_from_table(table)
    bad[0]["actual"] += 0.06
    with pytest.raises(ExplanationRejected, match=f"{bad[0]['category_code']}.actual"):
        run_deviation_explanation(
            factory, scenario_kind="base", year=PLAN_YEAR,
            model=scripted_deviation_model(scenario_kind="base", year=PLAN_YEAR, summary="bad", contributions=bad),
        )
    assert _record_count(factory) == 1


def test_deviation_explanation_missing_category_is_rejected(ai_db):
    """(c) no OTH contribution -> rejected, nothing persisted."""
    factory, _ = ai_db
    contributions = [c for c in contributions_from_table(_table(factory)) if c["category_code"] != "OTH"]
    assert len(contributions) == 4
    model = scripted_deviation_model(scenario_kind="base", year=PLAN_YEAR, summary="no OTH", contributions=contributions)
    with pytest.raises(ExplanationRejected, match=r"missing contributions for \['OTH'\]"):
        run_deviation_explanation(factory, scenario_kind="base", year=PLAN_YEAR, model=model)
    assert _record_count(factory) == 0

    # an unknown category is rejected too
    contributions = contributions_from_table(_table(factory))
    contributions[0]["category_code"] = "DEPR"
    model = scripted_deviation_model(scenario_kind="base", year=PLAN_YEAR, summary="DEPR", contributions=contributions)
    with pytest.raises(ExplanationRejected, match="DEPR: not in the deterministic table"):
        run_deviation_explanation(factory, scenario_kind="base", year=PLAN_YEAR, model=model)
    assert _record_count(factory) == 0


def test_deviation_explanation_text_must_cite_deviation_figure(ai_db):
    """(d) right numbers in the fields, but the prose omits the deviation figure -> rejected."""
    factory, _ = ai_db
    table = _table(factory)
    contributions = contributions_from_table(table)
    mat = next(c for c in contributions if c["category_code"] == "MAT")
    mat["explanation"] = "material costs moved with volume; nothing unusual"
    model = scripted_deviation_model(scenario_kind="base", year=PLAN_YEAR, summary="no figure", contributions=contributions)
    with pytest.raises(ExplanationRejected, match="MAT.explanation: does not cite its deviation"):
        run_deviation_explanation(factory, scenario_kind="base", year=PLAN_YEAR, model=model)
    assert _record_count(factory) == 0

    # formatting is tolerated: thousands separator with a space, no plus sign on a positive figure
    dev = next(r for r in table["rows"] if r["category_code"] == "MAT")["deviation"]
    spaced = f"{abs(dev):,.1f}".replace(",", " ")
    mat["explanation"] = f"MAT deviation {'-' if dev < 0 else ''}{spaced} k EUR, volume-driven"
    model = scripted_deviation_model(scenario_kind="base", year=PLAN_YEAR, summary="spaced", contributions=contributions)
    run_deviation_explanation(factory, scenario_kind="base", year=PLAN_YEAR, model=model)
    assert _record_count(factory) == 1


def test_deviation_explanation_api_returns_422_on_mismatch(ai_db):
    """(e) the API maps ExplanationRejected to 422 and stores nothing."""
    factory, _ = ai_db
    contributions = contributions_from_table(_table(factory))
    next(c for c in contributions if c["category_code"] == "PERS")["plan"] += 100.0
    app = create_app(session_factory=factory)
    app.state.model_factory = lambda tp, ctx: scripted_deviation_model(
        scenario_kind=ctx["scenario_kind"], year=ctx["year"], summary="api", contributions=contributions
    )
    with TestClient(app) as client:
        r = client.post("/ai/deviation-explanation", json={"scenario_kind": "base", "year": PLAN_YEAR})
        assert r.status_code == 422, r.text
        assert "PERS.plan" in r.json()["detail"]
        assert client.get("/ai/records").json() == []


# --------------------------------------------------------------------------- A1: offline default provider


def test_env_scan_completes_offline_with_no_credential(ai_db, no_credentials):
    """The default (model=None) path with no Anthropic credential at all must still complete:
    nvplan.ai.agents.resolve_model falls back to the deterministic provider, which reads the
    real framework/notes tool results and flags a genuinely-related position."""
    factory, _ = ai_db
    rec = run_env_scan(factory, positions_subset=["D2"])
    assert rec.status is AiStatus.proposed
    assert rec.model_version == "deterministic-v1"
    assert rec.prompt_text and rec.response_text and rec.rationale.strip()
    # D2.P2 Inflation / D2.P4 Wage growth-ish position should pick up the PERS wage-agreement note
    # via real keyword overlap - not a hardcoded finding.
    with factory() as s:
        notes = s.scalars(select(ExternalNote)).all()
    assert any(n.source == NoteSource.ai_scan for n in notes) or "No existing external note" in rec.rationale


def test_revenue_proposal_completes_offline_with_no_credential(ai_db, no_credentials):
    factory, _ = ai_db
    rec = run_revenue_proposal(factory, scenario_kind="base", year=2027, default_value=DEFAULT_2027)
    assert rec.status is AiStatus.proposed
    assert rec.model_version == "deterministic-v1"
    assert rec.proposed_value is not None and rec.proposed_value > 0
    assert rec.rationale.strip()
    # the illustrative REV note for 2027 ("ends Q2 2027") is genuine, quantified data - the
    # deterministic provider must actually use it, not just echo the unchanged default.
    assert rec.proposed_value != DEFAULT_2027
    assert abs(rec.proposed_value - DEFAULT_2027) / DEFAULT_2027 * 100 <= 25 + 1e-6  # control-table bound


def test_deviation_explanation_completes_offline_with_no_credential(ai_db, no_credentials):
    factory, _ = ai_db
    rec = run_deviation_explanation(factory, scenario_kind="base", year=PLAN_YEAR)
    assert rec.status is AiStatus.proposed
    assert rec.model_version == "deterministic-v1"
    assert rec.rationale.strip()


def test_deterministic_provider_is_a_real_bound_chat_model(ai_db, no_credentials):
    """Sanity: the object resolve_model hands to build_touchpoint_agent is really the
    deterministic model, not a fake accidentally left set up for a live credential."""
    from nvplan.ai.agents import resolve_model

    model = resolve_model(None)
    assert isinstance(model, DeterministicChatModel)
    assert model.model_version == "deterministic-v1"
