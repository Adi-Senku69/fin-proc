import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from nvplan.config import CATEGORY_CODES
from nvplan.db.models import (
    AiRecord,
    AiStatus,
    Category,
    CategoryDriver,
    CategoryKind,
    Derivation,
    Parameter,
    PlanPath,
    PlanValue,
    Scenario,
    ScenarioKind,
    Statement,
    StatementLine,
    Touchpoint,
)
from nvplan.db.session import seed_categories


def _scenario(session):
    s = Scenario(kind=ScenarioKind.base, label="base", created_by="test")
    session.add(s)
    session.flush()
    return s


def test_categories_seeded(session):
    cats = {c.code: c for c in session.scalars(select(Category)).all()}
    assert set(CATEGORY_CODES) <= set(cats)
    assert cats["REV"].kind is CategoryKind.revenue and cats["REV"].driver is None
    assert cats["PERS"].driver is CategoryDriver.revenue
    assert cats["OTH"].driver is CategoryDriver.investment
    assert [c for c in cats.values() if not c.is_component].__len__() == 5
    assert cats["DEPR"].is_component is True
    # idempotent
    assert seed_categories(session) == []
    assert len(session.scalars(select(Category)).all()) == 6


def test_plan_value_requires_derivation(session):
    scen = _scenario(session)
    rev = session.scalar(select(Category).where(Category.code == "REV"))
    session.add(PlanValue(scenario_id=scen.id, category_id=rev.id, year=2026, value=1.0, path=PlanPath.valorized))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_plan_value_with_derivation_ok_and_unique(session):
    scen = _scenario(session)
    rev = session.scalar(select(Category).where(Category.code == "REV"))
    d = Derivation(formula_text="x", inputs_json={}, parameters_json={}, parent_ids_json=[])
    session.add(d)
    session.flush()
    session.add(PlanValue(scenario_id=scen.id, category_id=rev.id, year=2026, value=1.0, path=PlanPath.valorized, derivation_id=d.id))
    session.flush()
    session.add(PlanValue(scenario_id=scen.id, category_id=rev.id, year=2026, value=2.0, path=PlanPath.valorized, derivation_id=d.id))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_plan_value_dangling_derivation_fk_rejected(session):
    scen = _scenario(session)
    rev = session.scalar(select(Category).where(Category.code == "REV"))
    session.add(PlanValue(scenario_id=scen.id, category_id=rev.id, year=2026, value=1.0, path=PlanPath.valorized, derivation_id=999))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_parameter_requires_derivation(session):
    mat = session.scalar(select(Category).where(Category.code == "MAT"))
    session.add(
        Parameter(category_id=mat.id, alpha=1.0, beta=0.1, r_squared=0.99, valorization_rate=0.02,
                  window_from=2021, window_to=2025, calc_version="v1")
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_statement_line_requires_derivation(session):
    scen = _scenario(session)
    session.add(StatementLine(scenario_id=scen.id, statement=Statement.bs, line_code="cash", year=2026, value=1.0, mapping_ref="bs_mapping.yaml"))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_ai_record_defaults_to_proposed(session):
    rec = AiRecord(touchpoint=Touchpoint.revenue_proposal, prompt_text="p", response_text="r", rationale="because", model_version="claude-opus-5")
    session.add(rec)
    session.commit()
    fetched = session.get(AiRecord, rec.id)
    assert fetched.status is AiStatus.proposed
    assert fetched.confirmed_by is None and fetched.confirmed_at is None
    assert fetched.created_at is not None
