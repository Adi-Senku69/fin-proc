"""A fresh, seeded platform database for one eval case (nvplan/ai/evals, work package A3).

Deliberately independent of ``tests/test_ai_fixtures.py``: this package is production code
under ``nvplan/``, not a test module, so it does not import from ``tests/`` (that would only
resolve when ``tests/`` happens to be on ``sys.path``, i.e. under pytest, which would make
``uv run python -m nvplan.ai.evals.runner`` - the manual comparison path the reference
harness supports - fragile for no reason). The seeding below mirrors
``test_ai_fixtures.seed_ai_db`` closely (same categories, same illustrative actuals, one base
scenario, one dummy derivation, plan values + parameters for 2025) because the eval cases
exercise the exact same production code path (``nvplan.ai.agents``/``nvplan.ai.assistant``)
the rest of the AI-layer test suite does; drift between the two would just mean two slightly
different fixtures testing the same thing, which is worse than one obvious duplication.

Every case gets its OWN on-disk sqlite file (see ``nvplan.ai.evals.runner.run_one``), never a
shared database - cases plant hostile rows and mutate ai_record/external_note state, and nothing
here is safe to share across cases.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bridge.db import init_platform_db
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
)
from nvplan.db.session import category_map, get_engine, init_db, seed_categories
from nvplan.ingest.actuals import ingest_actuals, load_actuals_csv

SessionFactory = Callable[[], Session]

# The historical year the seeded plan/actuals/parameters line up on - same value
# tests/test_ai_fixtures.py uses, so deviation-explanation cases have a real table to check
# figures against.
PLAN_YEAR = 2025
PLAN_FACTOR = 0.98


def _seed(session: Session) -> dict[str, float]:
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
    scenario = Scenario(kind=ScenarioKind.base, label="Base (eval)", created_by="eval")
    derivation = Derivation(formula_text="eval fixture: plan = actual * 0.98", inputs_json={}, parameters_json={})
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
                    calc_version="eval",
                    derivation_id=derivation.id,
                )
            )
    session.commit()
    return plans


def new_platform_db(db_path: Path) -> tuple[SessionFactory, dict[str, float]]:
    """One sqlite file at ``db_path``, both the finance tables and the provenance (brain)
    tables (``bridge.db.init_platform_db`` - the same helper ``tests/test_assistant.py`` uses),
    seeded exactly like ``test_ai_fixtures.ai_db``. Provenance tables are created unconditionally
    (cheap, additive) so a case can plant a ``Claim``/``Evidence`` row without a second fixture -
    this is what makes the brain-claim injection channel and the B2 extension seam (see
    ``cases.py``) exercisable without a live ``brain/`` tree."""
    engine = get_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    init_platform_db(init_db(engine))
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        plans = _seed(s)
    return factory, plans
