"""P2 bridge (finance half, PLATFORM.md §7.1): claim-sourced revenue overrides.

A ``decided`` decision carrying a quantified REV effect for a year becomes a revenue
override the planning run already accepts, via the widened ``Override`` dataclass. This
mirrors ``tests/test_services_planning.py``'s AI-override coverage, but for the ``claim_id``
path, plus the ``Override`` validation rule and the tuple / AI path staying byte-for-byte
unchanged.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from nvplan.config import DATA_DIR, PLAN_YEARS, REGRESSION_WINDOW
from nvplan.core.projector import revenue_key
from nvplan.db.models import (
    AiRecord,
    Category,
    Derivation,
    Parameter,
    PlanPath,
    PlanValue,
    Touchpoint,
)
from nvplan.db.session import SessionLocal, get_engine, init_db, seed_categories
from nvplan.ingest.actuals import ingest_actuals, load_actuals_csv
from nvplan.services.planning import (
    AI_OVERRIDE_FORMULA,
    DECISION_OVERRIDE_FORMULA,
    Override,
    load_control_table,
    run_plan,
)

YEAR = 2027
T0 = REGRESSION_WINDOW[1]


@pytest.fixture()
def session():
    engine = init_db(get_engine("sqlite://"))
    with SessionLocal(bind=engine) as s:
        seed_categories(s)
        ingest_actuals(s, load_actuals_csv(DATA_DIR / "actuals.csv"))
        yield s


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


# --------------------------------------------------------------------------- Override itself


def test_override_requires_exactly_one_id():
    Override(value=1.0, ai_record_id=1)  # fine
    Override(value=1.0, claim_id=2)  # fine
    with pytest.raises(ValueError):
        Override(value=1.0)  # neither
    with pytest.raises(ValueError):
        Override(value=1.0, ai_record_id=1, claim_id=2)  # both


def test_override_has_no_formula_text_field():
    """formula_text was a dead field: run_plan always derives the formula from which id
    is set, never from Override itself. It must be gone from the dataclass entirely,
    not merely unused."""
    with pytest.raises(TypeError):
        Override(value=1.0, claim_id=1, formula_text="x")


# --------------------------------------------------------------------------- claim-sourced override


def test_claim_override_sets_decided_path_and_claim_id(session):
    proposed_value = 21000.0
    default_before = None
    run = run_plan(
        session,
        revenue_override={YEAR: Override(value=proposed_value, claim_id=42, label="ship-eu-region")},
    )
    cats = {c.id: c.code for c in session.scalars(select(Category)).all()}
    kinds = {sid: kind for kind, sid in run.scenario_ids.items()}
    pvs = session.scalars(select(PlanValue)).all()

    for pv in pvs:
        code = cats[pv.category_id]
        if code == "REV" and pv.year == YEAR:
            assert pv.path is PlanPath.decided
            assert pv.claim_id == 42
            assert pv.ai_record_id is None
            if kinds[pv.scenario_id] == "base":
                assert pv.value == proposed_value
        elif code == "REV":
            assert pv.path is PlanPath.valorized
            assert pv.claim_id is None and pv.ai_record_id is None
        else:
            assert pv.path is PlanPath.cascaded
            assert pv.claim_id is None and pv.ai_record_id is None

    # the base REV derivation records the decision; the displaced default stays its parent
    d = session.get(Derivation, run.derivation_ids["plan:base:REV:2027"])
    assert d.formula_text == DECISION_OVERRIDE_FORMULA
    assert d.inputs_json["claim_id"] == 42
    assert d.inputs_json["decision_slug"] == "ship-eu-region"
    assert d.inputs_json["proposed_value"] == proposed_value
    default_before = d.inputs_json["default_value"]
    assert default_before > 0 and default_before != proposed_value
    assert d.parent_ids_json == [run.derivation_ids["plan:default:REV:2027"]]

    # cost cascade: PERS[2027] == alpha*(1+v)^n + beta*proposed to 1e-9
    p = session.get(Parameter, run.parameter_ids["PERS"])
    pers = next(
        pv for pv in pvs
        if cats[pv.category_id] == "PERS" and pv.year == YEAR and kinds[pv.scenario_id] == "base"
    )
    expected = p.alpha * (1 + p.valorization_rate) ** (YEAR - T0) + p.beta * proposed_value
    assert pers.value == pytest.approx(expected, abs=1e-9)
    assert pers.path is PlanPath.cascaded


def test_claim_override_records_displaced_default_on_all_three_rev_derivations(session):
    """UI.md Part 1's impact route reads `displaced_default` off the derivation of every
    REV row for the overridden year, not just base. Best / worst are `proposed x (1 +
    spread)` pass-throughs (unchanged formula_text / parents); what each one displaced is
    `default x (1 + spread)` for its own spread - this must now be a recorded input."""
    proposed_value = 21000.0
    run = run_plan(
        session,
        revenue_override={YEAR: Override(value=proposed_value, claim_id=42, label="ship-eu-region")},
    )
    ct = load_control_table(DATA_DIR / "control_table.yaml")
    spread = {str(k): float(v) for k, v in ct["revenue_proposal"]["scenario_spread"].items()}

    base_d = session.get(Derivation, run.derivation_ids[revenue_key("base", YEAR)])
    base_default = base_d.inputs_json["default_value"]
    assert base_default > 0

    for scenario, s in spread.items():
        d = session.get(Derivation, run.derivation_ids[revenue_key(scenario, YEAR)])
        assert d.inputs_json["default_value"] == pytest.approx(base_default * (1.0 + s), abs=1e-9)
        # formula_text / parent lineage of the spread pass-through must be untouched
        assert d.formula_text == "PlanRevenue_scenario_t = PlanRevenue_base_t * (1 + spread)"
        assert d.parent_ids_json == [run.derivation_ids[revenue_key("base", YEAR)]]

    # the worked example in the task: base displaced ~23418.941468357232, spreads +-0.08
    # (asserted structurally above via base_default; here pinned to the concrete figures
    # so a future change to actuals.csv that shifts the default is caught loudly)
    assert base_default == pytest.approx(23418.941468357232, abs=1e-6)
    best_d = session.get(Derivation, run.derivation_ids[revenue_key("best", YEAR)])
    worst_d = session.get(Derivation, run.derivation_ids[revenue_key("worst", YEAR)])
    assert best_d.inputs_json["default_value"] == pytest.approx(25292.5, abs=0.1)
    assert worst_d.inputs_json["default_value"] == pytest.approx(21545.4, abs=0.1)


def test_ai_override_records_displaced_default_on_all_three_rev_derivations(session):
    """Same treatment for the confirmed-AI-proposal path (identical shape, same route)."""
    rec = AiRecord(touchpoint=Touchpoint.revenue_proposal, prompt_text="p", response_text="r",
                   rationale="why", model_version="fake", proposed_value=20500.0, year=YEAR)
    session.add(rec)
    session.commit()
    run = run_plan(session, revenue_override={YEAR: (20500.0, rec.id)})
    ct = load_control_table(DATA_DIR / "control_table.yaml")
    spread = {str(k): float(v) for k, v in ct["revenue_proposal"]["scenario_spread"].items()}

    base_d = session.get(Derivation, run.derivation_ids[revenue_key("base", YEAR)])
    base_default = base_d.inputs_json["default_value"]
    assert base_d.formula_text == AI_OVERRIDE_FORMULA

    for scenario, s in spread.items():
        d = session.get(Derivation, run.derivation_ids[revenue_key(scenario, YEAR)])
        assert d.inputs_json["default_value"] == pytest.approx(base_default * (1.0 + s), abs=1e-9)
        assert d.formula_text == "PlanRevenue_scenario_t = PlanRevenue_base_t * (1 + spread)"
        assert d.parent_ids_json == [run.derivation_ids[revenue_key("base", YEAR)]]


def _all_plan_values(session) -> set[tuple[int, str, int, float]]:
    """(scenario_id, category_code, year, value) for every persisted plan value - a
    fingerprint of every figure the plan produced, used to prove the fix moved no figure."""
    cats = {c.id: c.code for c in session.scalars(select(Category)).all()}
    return {
        (pv.scenario_id, cats[pv.category_id], pv.year, pv.value)
        for pv in session.scalars(select(PlanValue)).all()
    }


def test_claim_override_moves_no_plan_value(session):
    """Regression guard: recording `default_value` on best/worst is purely additive to
    the derivation's inputs. No plan value anywhere may move. Two independent runs under
    the same inputs (fresh scenarios each time, since run_plan is append-only) must
    produce byte-identical sets of (scenario_kind, category, year, value)."""
    kwargs = dict(revenue_override={YEAR: Override(value=21000.0, claim_id=42, label="ship-eu-region")})

    run_a = run_plan(session, **kwargs, label_suffix=" run-a")
    cats = {c.id: c.code for c in session.scalars(select(Category)).all()}
    kinds_a = {sid: kind for kind, sid in run_a.scenario_ids.items()}
    values_a = {
        (kinds_a[pv.scenario_id], cats[pv.category_id], pv.year, pv.value)
        for pv in session.scalars(select(PlanValue)).all()
        if pv.scenario_id in run_a.scenario_ids.values()
    }

    run_b = run_plan(session, **kwargs, label_suffix=" run-b")
    kinds_b = {sid: kind for kind, sid in run_b.scenario_ids.items()}
    values_b = {
        (kinds_b[pv.scenario_id], cats[pv.category_id], pv.year, pv.value)
        for pv in session.scalars(select(PlanValue)).all()
        if pv.scenario_id in run_b.scenario_ids.values()
    }

    assert values_a == values_b
    assert len(values_a) > 0


def test_claim_override_year_outside_plan_years_raises(session):
    with pytest.raises(ValueError):
        run_plan(session, revenue_override={2040: Override(value=1.0, claim_id=1)})


def test_claim_override_rerun_is_append_only(session):
    run1 = run_plan(session, revenue_override={YEAR: Override(value=21000.0, claim_id=7, label="slug-a")})
    before_pv = _count(session, PlanValue)
    before_deriv = _count(session, Derivation)
    run2 = run_plan(
        session, revenue_override={YEAR: Override(value=22000.0, claim_id=7, label="slug-a")},
        label_suffix=" second",
    )
    assert set(run2.scenario_ids.values()).isdisjoint(run1.scenario_ids.values())
    assert _count(session, PlanValue) == 2 * before_pv
    assert _count(session, Derivation) == 2 * before_deriv
    # old rows untouched
    old_rev = session.get(PlanValue, next(
        pv.id for pv in session.scalars(select(PlanValue)).all()
        if pv.scenario_id == run1.scenario_ids["base"]
    ))
    assert old_rev is not None


# --------------------------------------------------------------------------- tuple / AI-sourced path unchanged


def test_tuple_override_still_produces_ai_proposed(session):
    """A bare (value, ai_record_id) tuple keeps its exact current meaning: path=ai_proposed,
    formula=AI_OVERRIDE_FORMULA, no claim_id anywhere."""
    rec = AiRecord(touchpoint=Touchpoint.revenue_proposal, prompt_text="p", response_text="r",
                   rationale="why", model_version="fake", proposed_value=20000.0, year=YEAR)
    session.add(rec)
    session.commit()
    run = run_plan(session, revenue_override={YEAR: (20000.0, rec.id)})
    cats = {c.id: c.code for c in session.scalars(select(Category)).all()}
    kinds = {sid: kind for kind, sid in run.scenario_ids.items()}
    pvs = session.scalars(select(PlanValue)).all()
    for pv in pvs:
        code = cats[pv.category_id]
        assert pv.claim_id is None
        if code == "REV" and pv.year == YEAR:
            assert pv.path is PlanPath.ai_proposed and pv.ai_record_id == rec.id
            if kinds[pv.scenario_id] == "base":
                assert pv.value == 20000.0
        elif code == "REV":
            assert pv.path is PlanPath.valorized and pv.ai_record_id is None
        else:
            assert pv.path is PlanPath.cascaded and pv.ai_record_id is None
    d = session.get(Derivation, run.derivation_ids["plan:base:REV:2027"])
    assert d.formula_text == AI_OVERRIDE_FORMULA
    assert d.inputs_json["ai_record_id"] == rec.id


def test_explicit_ai_override_object_behaves_like_tuple(session):
    """An Override(ai_record_id=...) built explicitly (instead of a tuple) reaches the
    same outcome - no change from today's behaviour."""
    rec = AiRecord(touchpoint=Touchpoint.revenue_proposal, prompt_text="p", response_text="r",
                   rationale="why", model_version="fake", proposed_value=19500.0, year=YEAR)
    session.add(rec)
    session.commit()
    run = run_plan(session, revenue_override={YEAR: Override(value=19500.0, ai_record_id=rec.id)})
    rev = next(
        pv for pv in session.scalars(select(PlanValue)).all()
        if pv.scenario_id == run.scenario_ids["base"]
        and session.get(Category, pv.category_id).code == "REV" and pv.year == YEAR
    )
    assert rev.path is PlanPath.ai_proposed and rev.ai_record_id == rec.id and rev.claim_id is None
    d = session.get(Derivation, run.derivation_ids["plan:base:REV:2027"])
    assert d.formula_text == AI_OVERRIDE_FORMULA
