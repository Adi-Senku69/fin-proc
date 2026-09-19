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
