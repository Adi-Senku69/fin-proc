"""run_plan: schema-enforced provenance, append-only reruns, equality with the pure core."""

from __future__ import annotations

import pandas as pd
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from nvplan.config import DATA_DIR, PLAN_YEARS, REGRESSION_WINDOW
from nvplan.core import DerivationLedger, to_wide
from nvplan.core.depreciation import depreciation_schedule
from nvplan.core.projector import project_all
from nvplan.core.regression import fit_all, revenue_default_path
from nvplan.db.models import (
    Category,
    Derivation,
    Parameter,
    PlanPath,
    PlanValue,
    Scenario,
    ScenarioKind,
    Statement,
    StatementLine,
)
from nvplan.db.session import SessionLocal, get_engine, init_db, seed_categories
from nvplan.ingest.actuals import ingest_actuals, load_actuals_csv
from nvplan.services.planning import KEY_FIELD, PlanRun, run_plan

CODES = ["REV", "MAT", "EXT", "PERS", "OTH", "DEPR"]
N_YEARS = PLAN_YEARS[1] - PLAN_YEARS[0] + 1


@pytest.fixture()
def session():
    engine = init_db(get_engine("sqlite://"))
    with SessionLocal(bind=engine) as s:
        seed_categories(s)
        ingest_actuals(s, load_actuals_csv(DATA_DIR / "actuals.csv"))
        yield s


@pytest.fixture()
def run(session) -> PlanRun:
    return run_plan(session, created_by="tester")


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def test_three_scenarios_and_row_counts(session, run):
    assert set(run.scenario_ids) == {"base", "best", "worst"}
    scenarios = session.scalars(select(Scenario)).all()
    assert {s.kind for s in scenarios} == {ScenarioKind.base, ScenarioKind.best, ScenarioKind.worst}
    assert all(s.created_by == "tester" for s in scenarios)
    assert run.n_plan_values == _count(session, PlanValue) == 3 * len(CODES) * N_YEARS
    # statement lines: pl 10 x 5, bs 8 x 6 (incl. opening 2025), cf 11 x 5 per scenario
    assert run.n_statement_lines == _count(session, StatementLine) == 3 * (10 * 5 + 8 * 6 + 11 * 5)
    per_stmt = dict(session.execute(
        select(StatementLine.statement, func.count()).group_by(StatementLine.statement)).all())
    assert per_stmt == {Statement.pl: 150, Statement.bs: 144, Statement.cf: 165}
    assert set(run.parameter_ids) == {"MAT", "EXT", "PERS", "OTH"}
    assert _count(session, Parameter) == 4
    assert _count(session, Derivation) == run.n_derivations


def test_every_number_has_a_derivation_and_parents_exist(session, run):
    ids = {d.id for d in session.scalars(select(Derivation)).all()}
    for pv in session.scalars(select(PlanValue)).all():
        assert pv.derivation_id in ids
    for sl in session.scalars(select(StatementLine)).all():
        assert sl.derivation_id in ids
    for p in session.scalars(select(Parameter)).all():
        assert p.derivation_id in ids
    for d in session.scalars(select(Derivation)).all():
        assert d.formula_text
        assert isinstance(d.parent_ids_json, list)
        for pid in d.parent_ids_json:
            assert pid in ids, (d.id, pid)
            assert pid < d.id  # parents were written first
        assert d.inputs_json[KEY_FIELD] in run.derivation_ids
        assert run.derivation_ids[d.inputs_json[KEY_FIELD]] == d.id
    # parent ids reproduce the ledger's parent keys
    inv = {v: k for k, v in run.derivation_ids.items()}
    d = session.get(Derivation, run.derivation_ids["plan:base:PERS:2028"])
    assert [inv[p] for p in d.parent_ids_json] == ["param:PERS", "plan:base:REV:2028"]
    d = session.get(Derivation, run.derivation_ids["stmt:base:bs:cash:2028"])
    assert [inv[p] for p in d.parent_ids_json] == ["stmt:base:cf:closing_cash:2028"]
    d = session.get(Derivation, run.derivation_ids["stmt:base:pl:depreciation:2027"])
    assert [inv[p] for p in d.parent_ids_json] == ["plan:base:DEPR:2027"]


def test_plan_value_without_derivation_is_rejected_by_schema(session, run):
    rev = session.scalar(select(Category).where(Category.code == "REV"))
    session.add(PlanValue(scenario_id=run.scenario_ids["base"], category_id=rev.id, year=2031, value=1.0,
                          path=PlanPath.valorized))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_paths_and_categories(session, run):
    cats = {c.id: c.code for c in session.scalars(select(Category)).all()}
    for pv in session.scalars(select(PlanValue)).all():
        code = cats[pv.category_id]
        assert pv.ai_record_id is None
        if code == "REV":
            assert pv.path is PlanPath.valorized
        else:
            assert pv.path is PlanPath.cascaded
    depr = session.scalar(select(Category).where(Category.code == "DEPR"))
    assert depr.is_component
    assert _count(session, PlanValue) and session.scalar(
        select(func.count()).select_from(PlanValue).where(PlanValue.category_id == depr.id)) == 3 * N_YEARS


def test_values_equal_pure_core(session, run):
    wide = to_wide(pd.read_csv(DATA_DIR / "actuals.csv"))
    ledger = DerivationLedger()
    fits = fit_all(wide, ("MAT", "EXT", "PERS", "OTH"), REGRESSION_WINDOW, ledger=ledger, depreciation=wide["DEPR"])
    sched = depreciation_schedule(pd.read_csv(DATA_DIR / "investment_plan.csv"), ledger=ledger)
    base = revenue_default_path(wide, REGRESSION_WINDOW, PLAN_YEARS, ledger=ledger)
    plan = project_all(fits, base, {"best": 0.08, "worst": -0.08}, t0=REGRESSION_WINDOW[1],
                       depreciation=sched.set_index("year")["depreciation"], ledger=ledger)

    cats = {c.id: c.code for c in session.scalars(select(Category)).all()}
    kinds = {sid: kind for kind, sid in run.scenario_ids.items()}
    db = {(kinds[pv.scenario_id], cats[pv.category_id], pv.year): pv.value
          for pv in session.scalars(select(PlanValue)).all()}
    assert len(db) == len(plan)
    for row in plan.itertuples(index=False):
        assert db[(row.scenario, row.category_code, row.year)] == pytest.approx(row.value, abs=1e-9), row

    for code, fit in fits.items():
        p = session.get(Parameter, run.parameter_ids[code])
        assert cats[p.category_id] == code
        assert (p.alpha, p.beta, p.r_squared, p.valorization_rate) == (
            fit.alpha, fit.beta, fit.r_squared, fit.valorization_rate)
        assert (p.window_from, p.window_to) == REGRESSION_WINDOW
        assert p.calc_version == "core-1.0"


def test_statement_lines_consistent(session, run):
    rows = session.scalars(select(StatementLine).where(StatementLine.scenario_id == run.scenario_ids["base"])).all()
    val = {(r.statement, r.line_code, r.year): r.value for r in rows}
    for y in range(PLAN_YEARS[0], PLAN_YEARS[1] + 1):
        assert val[(Statement.bs, "total_assets", y)] == pytest.approx(val[(Statement.bs, "total_liabilities_equity", y)], abs=1e-6)
        assert val[(Statement.bs, "cash", y)] == pytest.approx(val[(Statement.cf, "closing_cash", y)], abs=1e-9)
        assert val[(Statement.bs, "cash", y)] - val[(Statement.bs, "cash", y - 1)] == pytest.approx(
            val[(Statement.cf, "net_cash_flow", y)], abs=1e-9)
        assert val[(Statement.pl, "net_income", y)] == pytest.approx(
            val[(Statement.pl, "ebit", y)] - val[(Statement.pl, "tax", y)], abs=1e-9)
    assert val[(Statement.bs, "cash", 2025)] == 2500.0
    refs = {r.mapping_ref for r in rows}
    assert "opening_balance_sheet" in refs and "bs.assets.receivables" in refs and "pl.lines.revenue" in refs


def test_rerun_is_append_only(session, run):
    before = {
        "scenario": {s.id: (s.kind, s.label) for s in session.scalars(select(Scenario)).all()},
        "parameter": {p.id: (p.alpha, p.beta) for p in session.scalars(select(Parameter)).all()},
        "plan_value": {pv.id: pv.value for pv in session.scalars(select(PlanValue)).all()},
        "derivation": _count(session, Derivation),
        "statement_line": _count(session, StatementLine),
    }
    run2 = run_plan(session, created_by="tester", label_suffix=" second")
    assert set(run2.scenario_ids.values()).isdisjoint(run.scenario_ids.values())
    assert set(run2.parameter_ids.values()).isdisjoint(run.parameter_ids.values())
    assert _count(session, Scenario) == 6 and _count(session, Parameter) == 8
    assert _count(session, PlanValue) == 2 * before["plan_value"].__len__()
    assert _count(session, Derivation) == 2 * before["derivation"]
    assert _count(session, StatementLine) == 2 * before["statement_line"]
    # old rows untouched
    assert {s.id: (s.kind, s.label) for s in session.scalars(select(Scenario)).all() if s.id in before["scenario"]} == before["scenario"]
    assert {p.id: (p.alpha, p.beta) for p in session.scalars(select(Parameter)).all() if p.id in before["parameter"]} == before["parameter"]
    assert {pv.id: pv.value for pv in session.scalars(select(PlanValue)).all() if pv.id in before["plan_value"]} == before["plan_value"]
    labels = [s.label for s in session.scalars(select(Scenario)).all() if s.id in run2.scenario_ids.values()]
    assert all(lbl.endswith(" second") for lbl in labels)


def test_revenue_override_marks_rows_and_cascades(session):
    from nvplan.db.models import AiRecord, Touchpoint

    rec = AiRecord(touchpoint=Touchpoint.revenue_proposal, prompt_text="p", response_text="r", rationale="why",
                   model_version="fake", proposed_value=20000.0, year=2027)
    session.add(rec)
    session.commit()
    run = run_plan(session, revenue_override={2027: (20000.0, rec.id)})
    cats = {c.id: c.code for c in session.scalars(select(Category)).all()}
    kinds = {sid: kind for kind, sid in run.scenario_ids.items()}
    pvs = session.scalars(select(PlanValue)).all()
    for pv in pvs:
        code = cats[pv.category_id]
        if code == "REV" and pv.year == 2027:
            assert pv.path is PlanPath.ai_proposed and pv.ai_record_id == rec.id
            if kinds[pv.scenario_id] == "base":
                assert pv.value == 20000.0
        elif code == "REV":
            assert pv.path is PlanPath.valorized and pv.ai_record_id is None
        else:
            assert pv.path is PlanPath.cascaded and pv.ai_record_id is None
    d = session.get(Derivation, run.derivation_ids["plan:base:REV:2027"])
    assert d.formula_text == "confirmed AI proposal"
    assert d.inputs_json["proposed_value"] == 20000.0 and d.inputs_json["ai_record_id"] == rec.id
    assert d.inputs_json["default_value"] > 0 and d.inputs_json["default_value"] != 20000.0
    assert d.parent_ids_json == [run.derivation_ids["plan:default:REV:2027"]]
    with pytest.raises(ValueError):
        run_plan(session, revenue_override={2040: (1.0, rec.id)})


def test_missing_parent_is_an_error():
    """A derivation whose parent exists nowhere (core or statements) is rejected, not skipped."""
    from nvplan.core.statements import LineDerivation
    from nvplan.services.planning import _merged_records

    ledger = DerivationLedger()
    ledger.add("param:MAT", "ok")
    ledger.add("plan:base:MAT:2026", "x", parents=("param:MAT", "ghost:1"))
    with pytest.raises(KeyError, match="ghost:1"):
        _merged_records(ledger, {})
    # a statements record may point into the core ledger; an unknown parent there is an error too
    good = DerivationLedger()
    good.add("plan:base:MAT:2026", "x")
    stmts = type("S", (), {})()
    stmts.derivations = {"stmt:base:pl:material:2026": LineDerivation(
        "stmt:base:pl:material:2026", "material = MAT[2026]", {}, {}, ("plan:base:MAT:2026",))}
    assert set(_merged_records(good, {"base": stmts})) == {"plan:base:MAT:2026", "stmt:base:pl:material:2026"}
    stmts.derivations["stmt:base:pl:ebit:2026"] = LineDerivation(
        "stmt:base:pl:ebit:2026", "ebit", {}, {}, ("stmt:base:pl:revenue:2026",))
    with pytest.raises(KeyError, match="stmt:base:pl:revenue:2026"):
        _merged_records(good, {"base": stmts})


def test_failed_run_writes_nothing(monkeypatch, session):
    """Persistence is one transaction: a failure mid-way leaves no partial rows."""
    import nvplan.services.planning as planning

    original = planning._merged_records

    def broken(ledger, statements):
        records = dict(original(ledger, statements))
        from nvplan.core import Derivation as D
        records["plan:base:MAT:2026"] = D(key="plan:base:MAT:2026", formula_text="x", parents=("ghost:1",))
        return records  # unresolved parent slips past the merge check -> insertion must still fail

    monkeypatch.setattr(planning, "_merged_records", broken)
    with pytest.raises(KeyError, match="ghost"):
        run_plan(session)
    assert _count(session, Derivation) == 0 and _count(session, Scenario) == 0
    assert _count(session, Parameter) == 0 and _count(session, PlanValue) == 0
    monkeypatch.undo()
    run_plan(session)  # the session is usable afterwards
    assert _count(session, Scenario) == 3
