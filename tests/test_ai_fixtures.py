"""Shared seeding for the AI-layer tests (no tests in here).

Seeds a file-based SQLite (tool calls run in threads, so an in-memory DB with a
shared connection is not safe) with: categories, actuals from
data/illustrative/actuals.csv, the four illustrative external notes, one base
scenario, one dummy derivation, plan_values for REV/MAT/EXT/PERS/OTH for 2025
(actual * 0.98) and one parameter row per cost category.
"""

from __future__ import annotations

import pandas as pd
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from nvplan.config import DATA_DIR
from nvplan.db.models import (
    Actual,
    Derivation,
    ExternalNote,
    NoteSource,
    Parameter,
    PlanPath,
    PlanValue,
    Scenario,
    ScenarioKind,
    StatementLine,
)
from nvplan.db.session import category_map, get_engine, init_db, seed_categories
from nvplan.ingest.actuals import ingest_actuals, load_actuals_csv

PLAN_YEAR = 2025
PLAN_FACTOR = 0.98
FORBIDDEN_TABLES = [PlanValue, StatementLine, Actual, Parameter, Derivation, Scenario]


def seed_ai_db(session: Session) -> dict[str, float]:
    """Seed and return {category_code: plan_value_2025}."""
    seed_categories(session)
    ingest_actuals(session, load_actuals_csv(DATA_DIR / "actuals.csv"))
    cats = category_map(session)
    for row in pd.read_csv(DATA_DIR / "external_notes.csv").itertuples(index=False):
        session.add(
            ExternalNote(
                category_id=cats[row.category_code].id,
                year=int(row.year),
                text=row.text,
                author=row.author,
                source=NoteSource(row.source),
            )
        )
    scenario = Scenario(kind=ScenarioKind.base, label="Base (test)", created_by="test")
    derivation = Derivation(formula_text="test fixture: plan = actual * 0.98", inputs_json={}, parameters_json={})
    session.add_all([scenario, derivation])
    session.flush()
    actuals = {a.category_id: a.value for a in session.scalars(select(Actual).where(Actual.year == PLAN_YEAR)).all()}
    plans: dict[str, float] = {}
    for code in ["REV", "MAT", "EXT", "PERS", "OTH"]:
        cat = cats[code]
        plans[code] = round(actuals[cat.id] * PLAN_FACTOR, 1)
        session.add(
            PlanValue(
                scenario_id=scenario.id,
                category_id=cat.id,
                year=PLAN_YEAR,
                value=plans[code],
                path=PlanPath.valorized,
                derivation_id=derivation.id,
            )
        )
        if code != "REV":
            session.add(
                Parameter(
                    category_id=cat.id,
                    alpha=100.0,
                    beta=0.1,
                    r_squared=0.97,
                    valorization_rate=0.02,
                    window_from=2020,
                    window_to=2024,
                    calc_version="test",
                    derivation_id=derivation.id,
                )
            )
    session.commit()
    return plans


def forbidden_counts(session_factory) -> dict[str, int]:
    with session_factory() as s:
        return {t.__tablename__: s.scalar(select(func.count()).select_from(t)) for t in FORBIDDEN_TABLES}


@pytest.fixture()
def ai_db(tmp_path):
    """(session_factory, plans_2025) over a seeded file-based SQLite."""
    engine = get_engine(f"sqlite:///{tmp_path / 'ai_test.db'}", connect_args={"check_same_thread": False})
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        plans = seed_ai_db(s)
    return factory, plans
