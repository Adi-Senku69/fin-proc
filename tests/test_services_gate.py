"""The human confirmation gate: nothing enters the plan unconfirmed; rejection leaves no trace."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from nvplan.config import DATA_DIR
from nvplan.db.models import (
    AiRecord,
    AiStatus,
    Category,
    Parameter,
    PlanPath,
    PlanValue,
    Scenario,
    Touchpoint,
)
from nvplan.db.session import SessionLocal, get_engine, init_db, seed_categories
from nvplan.ingest.actuals import ingest_actuals, load_actuals_csv
from nvplan.services.gate import GateError, confirm_proposal, reject_proposal
from nvplan.services.planning import run_plan
from nvplan.services.trace import find_plan_value, render_trace, trace_plan_value

PROMPT = "You are the revenue advisor.\nPropose 2027 revenue and cite a note."


@pytest.fixture()
def session():
    engine = init_db(get_engine("sqlite://"))
    with SessionLocal(bind=engine) as s:
        seed_categories(s)
        ingest_actuals(s, load_actuals_csv(DATA_DIR / "actuals.csv"))
        yield s


@pytest.fixture()
def first_run(session):
    return run_plan(session, created_by="tester")


@pytest.fixture()
def proposal(session, first_run) -> AiRecord:
    default = find_plan_value(session, scenario_kind="base", category_code="REV", year=2027).value
    rev = session.scalar(select(Category).where(Category.code == "REV"))
    rec = AiRecord(
        touchpoint=Touchpoint.revenue_proposal, prompt_text=PROMPT, response_text='{"proposed_value": ...}',
        rationale="Major client contract ends Q2 2027 (note #1); -10 % vs valorized default.",
        model_version="fake-model", proposed_value=0.9 * default, scenario_id=first_run.scenario_ids["base"],
        category_id=rev.id, year=2027,
    )
    session.add(rec)
    session.commit()
    assert rec.status is AiStatus.proposed
    return rec


def _count(session, model, **where) -> int:
    stmt = select(func.count()).select_from(model)
    for k, v in where.items():
        stmt = stmt.where(getattr(model, k) == v)
    return session.scalar(stmt)


def test_proposal_does_not_enter_plan_until_confirmed(session, first_run, proposal):
    assert _count(session, PlanValue, ai_record_id=proposal.id) == 0
    assert _count(session, PlanValue, path=PlanPath.ai_proposed) == 0


def test_confirm_reruns_with_proposal(session, first_run, proposal):
    proposed = proposal.proposed_value
    run2 = confirm_proposal(session, proposal.id, confirmed_by="alice")
    rec = session.get(AiRecord, proposal.id)
    assert rec.status is AiStatus.confirmed and rec.confirmed_by == "alice" and rec.confirmed_at is not None
    assert run2 is not None and set(run2.scenario_ids.values()).isdisjoint(first_run.scenario_ids.values())
    assert _count(session, Scenario) == 6 and _count(session, Parameter) == 8

    rev = find_plan_value(session, scenario_kind="base", category_code="REV", year=2027)
    assert rev.scenario_id == run2.scenario_ids["base"]
    assert rev.value == proposed and rev.path is PlanPath.ai_proposed and rev.ai_record_id == proposal.id
    # other years of the new run stay valorized; nothing of the first run was touched
    rev26 = find_plan_value(session, scenario_kind="base", category_code="REV", year=2026)
    assert rev26.path is PlanPath.valorized and rev26.ai_record_id is None
    old = find_plan_value(session, scenario_kind="base", category_code="REV", year=2027,
                          scenario_id=first_run.scenario_ids["base"])
    assert old.path is PlanPath.valorized and old.ai_record_id is None and old.value != proposed

    # the cascade: PERS 2027 == alpha*(1+v)^2 + beta*proposed with the NEW parameter row
    p = session.get(Parameter, run2.parameter_ids["PERS"])
    pers = find_plan_value(session, scenario_kind="base", category_code="PERS", year=2027)
    expected = p.alpha * (1 + p.valorization_rate) ** (2027 - 2025) + p.beta * proposed
    assert pers.value == pytest.approx(expected, rel=1e-12)
    assert pers.path is PlanPath.cascaded and pers.ai_record_id is None
    # and it differs from the unconfirmed plan by beta * (proposed - default)
    old_pers = find_plan_value(session, scenario_kind="base", category_code="PERS", year=2027,
                               scenario_id=first_run.scenario_ids["base"])
    assert pers.value - old_pers.value == pytest.approx(p.beta * (proposed - old.value), abs=1e-9)
    # best / worst revenue of 2027 follow the proposal
    best = find_plan_value(session, scenario_kind="best", category_code="REV", year=2027)
    assert best.value == pytest.approx(1.08 * proposed, rel=1e-12) and best.ai_record_id == proposal.id

    # trace of PERS 2027 shows the ai block with the literal prompt and the confirmer
    tree = trace_plan_value(session, pers.id)
    rev_node = tree.find(key="plan:base:REV:2027")
    assert rev_node is not None and rev_node.path == "ai_proposed"
    assert rev_node.formula_text == "confirmed AI proposal"
    assert rev_node.inputs["proposed_value"] == proposed and rev_node.inputs["ai_record_id"] == proposal.id
    assert rev_node.ai["prompt_text"] == PROMPT and rev_node.ai["confirmed_by"] == "alice"
    assert rev_node.ai["status"] == "confirmed" and rev_node.ai["rationale"].startswith("Major client")
    assert rev_node.find(key="plan:default:REV:2027") is not None  # the replaced default stays traceable
    text = render_trace(tree)
    print("\n" + text)
    assert "confirmed_by=alice" in text and "Propose 2027 revenue" in text and "ai_proposed" in text


def test_confirm_twice_raises(session, first_run, proposal):
    confirm_proposal(session, proposal.id, confirmed_by="alice")
    n_scen = _count(session, Scenario)
    with pytest.raises(GateError):
        confirm_proposal(session, proposal.id, confirmed_by="bob")
    assert _count(session, Scenario) == n_scen
    assert session.get(AiRecord, proposal.id).confirmed_by == "alice"


def test_confirm_without_rerun(session, first_run, proposal):
    assert confirm_proposal(session, proposal.id, confirmed_by="alice", rerun=False) is None
    assert session.get(AiRecord, proposal.id).status is AiStatus.confirmed
    assert _count(session, Scenario) == 3
    assert _count(session, PlanValue, ai_record_id=proposal.id) == 0


def test_reject_leaves_no_trace_in_figures(session, first_run, proposal):
    n_pv, n_scen = _count(session, PlanValue), _count(session, Scenario)
    rec = reject_proposal(session, proposal.id, rejected_by="alice")
    assert rec.status is AiStatus.rejected and rec.confirmed_by == "alice"
    assert _count(session, PlanValue) == n_pv and _count(session, Scenario) == n_scen
    assert _count(session, PlanValue, ai_record_id=proposal.id) == 0
    assert _count(session, PlanValue, path=PlanPath.ai_proposed) == 0
    with pytest.raises(GateError):
        confirm_proposal(session, proposal.id, confirmed_by="bob")
    with pytest.raises(GateError):
        reject_proposal(session, proposal.id, rejected_by="bob")


def test_gate_validations(session, first_run):
    with pytest.raises(LookupError):
        confirm_proposal(session, 999, confirmed_by="alice")
    scan = AiRecord(touchpoint=Touchpoint.env_scan, prompt_text="p", response_text="r", rationale="x",
                    model_version="fake")
    session.add(scan)
    session.commit()
    with pytest.raises(GateError):
        confirm_proposal(session, scan.id, confirmed_by="alice")
    incomplete = AiRecord(touchpoint=Touchpoint.revenue_proposal, prompt_text="p", response_text="r",
                          rationale="x", model_version="fake")
    session.add(incomplete)
    session.commit()
    with pytest.raises(GateError):
        confirm_proposal(session, incomplete.id, confirmed_by="alice")
    assert session.get(AiRecord, incomplete.id).status is AiStatus.proposed
    with pytest.raises(GateError):
        confirm_proposal(session, incomplete.id, confirmed_by="")
