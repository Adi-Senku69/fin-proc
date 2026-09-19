"""The conversational assistant (UI.md Part 3): the backstop scan, the verification pass,
and the endpoint - all run offline against the scripted fake model."""

from __future__ import annotations

import json
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from bridge.db import init_platform_db
from nvplan.ai import AnswerRejected, ask, loose_figures
from nvplan.ai.fake import scripted_assistant_model
from nvplan.api.app import create_app
from nvplan.db.models import AiRecord, PlanValue, Touchpoint
from nvplan.db.session import category_map, get_engine, init_db
from provenance.models import Claim, ClaimKind
from test_ai_fixtures import PLAN_YEAR, seed_ai_db  # noqa: F401 (reused seeding helper)


@pytest.fixture()
def assistant_db(tmp_path):
    """A file-based SQLite with both nvplan and provenance tables (like ``bridge.db``),
    seeded like ``test_ai_fixtures.ai_db`` plus one decided claim with a quantified effect -
    enough to exercise a "claim" figure/segment citation without a real ``brain/`` tree."""
    engine = get_engine(f"sqlite:///{tmp_path / 'assistant_test.db'}", connect_args={"check_same_thread": False})
    init_platform_db(init_db(engine))
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        plans = seed_ai_db(s)
        claim = Claim(
            kind=ClaimKind.decision,
            slug="2026-09-20-sunset-legacy-import",
            title="Sunset the legacy import",
            status="decided",
            date=date(2026, 9, 20),
            effect_json={"category": "REV", "year": 2027, "value": 22_900.0, "unit": "kEUR"},
        )
        s.add(claim)
        s.commit()
        cats = category_map(s)
        rev_pv = s.scalar(
            select(PlanValue).where(PlanValue.category_id == cats["REV"].id, PlanValue.year == PLAN_YEAR)
        )
        ids = {"claim_id": claim.id, "plan_value_id": rev_pv.id, "plan_value_value": rev_pv.value}
    return factory, plans, ids


@pytest.fixture()
def no_credentials(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("nvplan.ai.agents._ant_cli", lambda: None)
    return None


def _records(factory) -> list[AiRecord]:
    with factory() as s:
        return list(s.scalars(select(AiRecord)).all())


# --------------------------------------------------------------------------- the backstop scan (pure function)


def test_loose_figures_flags_money_and_percentages():
    assert loose_figures("Costs came in around €1,245.7 k this year.") == ["1,245.7"]
    assert loose_figures("Margin improved by 12.5% year over year.") == ["12.5%"]
    assert loose_figures("A plain 500 k EUR swing.") == ["500"]
    assert loose_figures("A negative -245.7 swing.") == ["-245.7"]


def test_loose_figures_allows_years_in_window_or_horizon():
    assert loose_figures("In 2025 actuals landed above plan.") == []
    assert loose_figures("The horizon runs 2026-2030.") == []
    assert loose_figures("A year outside both windows, 2031, is not free.") == ["2031"]


def test_loose_figures_allows_small_counts_digit_or_spelled():
    assert loose_figures("There are 3 scenarios in the grid.") == []
    assert loose_figures("There are three scenarios in the grid.") == []
    assert loose_figures("500 categories would not be a count.") == ["500"]


def test_loose_figures_allows_identifiers_and_slugs():
    assert loose_figures("See param:PERS for the cost driver.") == []
    assert loose_figures("The decision 2026-09-20-sunset-legacy-import is decided.") == []
    assert loose_figures("param:PERS moved by 4.5% though.") == ["4.5%"]


def test_loose_figures_empty_text_is_clean():
    assert loose_figures("") == []
    assert loose_figures("No numbers here at all.") == []


# --------------------------------------------------------------------------- ask(): verify-before-persist


def test_ask_well_formed_answer_verifies_and_persists_one_record(assistant_db):
    factory, plans, ids = assistant_db
    segments = [
        {"type": "text", "text": f"In {PLAN_YEAR + 1} the plan for revenue is:"},
        {
            "type": "figure",
            "label": "Revenue plan",
            "value": ids["plan_value_value"],
            "unit": "k EUR",
            "ref": {"kind": "plan_value", "id": ids["plan_value_id"]},
        },
        {"type": "claim", "claim_id": ids["claim_id"], "title": "Sunset the legacy import", "status": "decided"},
    ]
    model = scripted_assistant_model(segments=segments)

    answer = ask(factory, "What is the revenue plan, and what decision affects it?", model=model)

    assert answer.ai_record_id > 0
    assert [s.type for s in answer.segments] == ["text", "figure", "claim"]
    assert answer.usage == {}  # the fake model reports no usage_metadata
    recs = _records(factory)
    assert len(recs) == 1
    rec = recs[0]
    assert rec.id == answer.ai_record_id
    # Touchpoint.deviation_explanation is reused (no `assistant` enum member is in file
    # ownership); the rationale prefix records the distinction (see nvplan.ai.assistant doc).
    assert rec.touchpoint is Touchpoint.deviation_explanation
    assert rec.rationale.startswith("[assistant] ")
    assert rec.status.value == "proposed"
    assert "---USER---" in rec.prompt_text
    assert "---STRUCTURED---" in rec.response_text
    assert rec.call_log_json is not None


def test_figure_value_disagreeing_with_row_is_rejected_and_nothing_persisted(assistant_db):
    factory, plans, ids = assistant_db
    segments = [
        {
            "type": "figure",
            "label": "Revenue plan",
            "value": ids["plan_value_value"] + 5.0,
            "unit": "k EUR",
            "ref": {"kind": "plan_value", "id": ids["plan_value_id"]},
        }
    ]
    model = scripted_assistant_model(segments=segments)

    with pytest.raises(AnswerRejected, match="vs stored"):
        ask(factory, "What is the revenue plan?", model=model)
    assert _records(factory) == []


def test_dangling_ref_is_rejected_and_nothing_persisted(assistant_db):
    factory, plans, ids = assistant_db
    segments = [
        {"type": "figure", "label": "Ghost", "value": 1.0, "unit": "k EUR", "ref": {"kind": "plan_value", "id": 999999}}
    ]
    model = scripted_assistant_model(segments=segments)

    with pytest.raises(AnswerRejected, match="does not resolve"):
        ask(factory, "q", model=model)
    assert _records(factory) == []


def test_loose_money_figure_in_prose_is_rejected(assistant_db):
    factory, plans, ids = assistant_db
    segments = [{"type": "text", "text": "Costs came in around €1,245.7 k this year, well above plan."}]
    model = scripted_assistant_model(segments=segments)

    with pytest.raises(AnswerRejected, match=r"1,245\.7"):
        ask(factory, "how are costs?", model=model)
    assert _records(factory) == []


def test_loose_percentage_figure_in_prose_is_rejected(assistant_db):
    factory, plans, ids = assistant_db
    segments = [{"type": "text", "text": "Margin improved by 12.5% year over year."}]
    model = scripted_assistant_model(segments=segments)

    with pytest.raises(AnswerRejected, match="12.5%"):
        ask(factory, "how is margin?", model=model)
    assert _records(factory) == []


def test_year_in_prose_is_allowed(assistant_db):
    factory, plans, ids = assistant_db
    segments = [{"type": "text", "text": f"Actuals through {PLAN_YEAR} are loaded; nothing else to report."}]
    model = scripted_assistant_model(segments=segments)

    answer = ask(factory, "what actuals are loaded?", model=model)
    assert answer.segments[0].text.strip()
    assert len(_records(factory)) == 1


def test_identifier_and_decision_slug_in_prose_are_allowed(assistant_db):
    factory, plans, ids = assistant_db
    segments = [
        {
            "type": "text",
            "text": "param:PERS is the cost line that decision 2026-09-20-sunset-legacy-import affects.",
        }
    ]
    model = scripted_assistant_model(segments=segments)

    answer = ask(factory, "what does the decision affect?", model=model)
    assert len(answer.segments) == 1
    assert len(_records(factory)) == 1


def test_claim_segment_with_bad_id_is_rejected(assistant_db):
    factory, plans, ids = assistant_db
    segments = [{"type": "claim", "claim_id": 424242, "title": "Nonexistent", "status": "decided"}]
    model = scripted_assistant_model(segments=segments)

    with pytest.raises(AnswerRejected, match="does not exist"):
        ask(factory, "q", model=model)
    assert _records(factory) == []


def test_parameter_figure_matches_any_of_its_fields(assistant_db):
    """A "parameter" ref has no single value column; it passes when the figure matches ANY
    of alpha/beta/R^2/valorization_rate within tolerance."""
    from nvplan.ai.tools import make_read_tools

    factory, plans, ids = assistant_db
    tool = {t.name: t for t in make_read_tools(factory)}["get_parameters"]
    row = next(r for r in json.loads(tool.invoke({}))["rows"] if r["category_code"] == "MAT")
    segments = [
        {
            "type": "figure",
            "label": "MAT beta",
            "value": row["beta"],
            "unit": "ratio",
            "ref": {"kind": "parameter", "id": row["parameter_id"]},
        }
    ]
    model = scripted_assistant_model(segments=segments)

    answer = ask(factory, "what is MAT's beta?", model=model)
    assert len(answer.segments) == 1
    assert len(_records(factory)) == 1


# --------------------------------------------------------------------------- tools return citable ids


def test_every_new_tool_returns_the_id_needed_to_cite_it(assistant_db):
    from nvplan.ai.tools import make_read_tools

    factory, plans, ids = assistant_db
    tools = {t.name: t for t in make_read_tools(factory)}

    pv = json.loads(tools["get_plan_value"].invoke({"scenario_kind": "base", "category_code": "REV", "year": PLAN_YEAR}))
    assert pv["plan_value_id"] == ids["plan_value_id"]

    grid = json.loads(tools["get_plan_values"].invoke({"scenario_kind": "base"}))
    assert all("plan_value_id" in r for r in grid["rows"])

    params = json.loads(tools["get_parameters"].invoke({}))
    assert all("parameter_id" in r for r in params["rows"])

    # every category's plan_value shares one derivation in this fixture (test_ai_fixtures.seed_ai_db);
    # regardless of which one the lineage lookup resolves to, its own kind carries its own id.
    trace = json.loads(tools["get_trace"].invoke({"plan_value_id": ids["plan_value_id"]}))
    assert trace["kind"] == "plan_value" and isinstance(trace["ref_id"], int)

    decisions = json.loads(tools["get_decisions"].invoke({}))
    assert any(r["claim_id"] == ids["claim_id"] for r in decisions["rows"])

    one = json.loads(tools["get_decision"].invoke({"claim_id": ids["claim_id"]}))
    assert one["claim_id"] == ids["claim_id"] and one["effect"]["value"] == pytest.approx(22_900.0)

    impact = json.loads(tools["get_claim_impact"].invoke({"claim_id": ids["claim_id"]}))
    assert impact == {"rows": []}  # this claim drove no plan value in this fixture

    backtest = json.loads(tools["get_backtest_summary"].invoke({}))
    assert "error" in backtest or "summary" in backtest  # context-only; no id expected


# --------------------------------------------------------------------------- endpoint


def test_endpoint_returns_422_with_failures_on_rejection(assistant_db):
    factory, plans, ids = assistant_db
    app = create_app(session_factory=factory)
    bad_segments = [{"type": "text", "text": "Costs were 45% higher than plan."}]
    app.state.model_factory = lambda tp, ctx: scripted_assistant_model(segments=bad_segments)  # noqa: ARG005
    with TestClient(app) as client:
        r = client.post("/assistant/ask", json={"question": "how are costs?"})
        assert r.status_code == 422, r.text
        assert "45%" in r.json()["detail"]
        assert client.get("/ai/records").json() == []


def test_endpoint_returns_200_with_segments_and_usage_on_success(assistant_db):
    factory, plans, ids = assistant_db
    app = create_app(session_factory=factory)
    good_segments = [
        {
            "type": "figure",
            "label": "Revenue plan",
            "value": ids["plan_value_value"],
            "unit": "k EUR",
            "ref": {"kind": "plan_value", "id": ids["plan_value_id"]},
        }
    ]
    app.state.model_factory = lambda tp, ctx: scripted_assistant_model(segments=good_segments)  # noqa: ARG005
    with TestClient(app) as client:
        r = client.post("/assistant/ask", json={"question": "what is revenue?"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["segments"][0]["type"] == "figure"
        assert body["segments"][0]["ref"] == {"kind": "plan_value", "id": ids["plan_value_id"]}
        assert body["ai_record_id"] > 0
        assert body["usage"] == {}
        assert len(client.get("/ai/records").json()) == 1


def test_endpoint_503_without_model_or_credentials(assistant_db, no_credentials):
    factory, plans, ids = assistant_db
    app = create_app(session_factory=factory)  # no model_factory injected
    with TestClient(app) as client:
        r = client.post("/assistant/ask", json={"question": "what is revenue?"})
        assert r.status_code == 503, r.text
        assert "ANTHROPIC_API_KEY" in r.json()["detail"]
        assert client.get("/ai/records").json() == []
