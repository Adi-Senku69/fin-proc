"""Tests for bridge.db.init_platform_db and bridge.lookup.make_derivation_lookup
(PLATFORM.md §7, §7.1's "money informs decisions" direction)."""

from __future__ import annotations

import pytest

pytest.importorskip("provenance", reason="provenance/ (Phase P0) is not present yet")

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from nvplan.config import DATA_DIR
from nvplan.db.models import Category, Derivation
from nvplan.db.session import seed_categories
from nvplan.ingest.actuals import ingest_actuals, load_actuals_csv
from nvplan.services.planning import run_plan

from provenance.models import Claim, ClaimKind

from bridge.db import init_platform_db
from bridge.lookup import make_derivation_lookup


@pytest.fixture()
def platform_engine():
    from provenance.models import get_engine

    engine = get_engine("sqlite:///:memory:")
    init_platform_db(engine)
    return engine


@pytest.fixture()
def platform_session(platform_engine):
    with Session(platform_engine) as session:
        yield session


@pytest.fixture()
def planned_session(platform_session):
    """A platform session with categories seeded, actuals ingested and one plan run -
    i.e. real derivation rows to resolve against."""
    seed_categories(platform_session)
    ingest_actuals(platform_session, load_actuals_csv(DATA_DIR / "actuals.csv"))
    run_plan(platform_session, created_by="tester")
    return platform_session


# --------------------------------------------------------------------------- init_platform_db


class TestInitPlatformDb:
    def test_creates_both_sets_of_tables(self, platform_engine):
        names = set(inspect(platform_engine).get_table_names())
        nvplan_tables = {"category", "actual", "derivation", "parameter", "scenario", "plan_value",
                        "external_note", "ai_record", "statement_line"}
        provenance_tables = {"claim", "evidence", "claim_link"}
        assert nvplan_tables <= names
        assert provenance_tables <= names

    def test_is_idempotent(self, platform_engine):
        init_platform_db(platform_engine)  # must not raise or drop data on a second call
        names = set(inspect(platform_engine).get_table_names())
        assert {"claim", "category"} <= names

    def test_one_session_queries_across_both_metadatas(self, platform_session):
        seed_categories(platform_session)
        platform_session.add(Claim(kind=ClaimKind.decision, slug="probe-claim"))
        platform_session.flush()

        cats = platform_session.execute(select(Category)).scalars().all()
        claims = platform_session.execute(select(Claim)).scalars().all()
        assert len(cats) >= 5
        assert len(claims) == 1
        assert claims[0].slug == "probe-claim"


# --------------------------------------------------------------------------- make_derivation_lookup


class TestMakeDerivationLookup:
    def test_real_key_resolves_to_the_right_derivation_after_a_plan_run(self, planned_session):
        lookup = make_derivation_lookup(planned_session)
        resolved_id = lookup("param:PERS")
        assert resolved_id is not None

        row = planned_session.get(Derivation, resolved_id)
        assert row is not None
        assert row.inputs_json["_key"] == "param:PERS"

    def test_unknown_key_returns_none_without_raising(self, planned_session):
        lookup = make_derivation_lookup(planned_session)
        assert lookup("no:such:key") is None
        assert lookup("still-unknown") is None  # a second miss must not raise either

    def test_repeated_lookups_of_the_same_key_are_consistent(self, planned_session):
        lookup = make_derivation_lookup(planned_session)
        first = lookup("param:PERS")
        second = lookup("param:PERS")  # cached path
        assert first == second is not None
