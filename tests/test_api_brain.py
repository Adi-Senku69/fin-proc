"""Brain / bridge API endpoints (PLATFORM.md §7, §7.1; UI.md Part 1) end-to-end with
TestClient on a file-based SQLite, exactly like tests/test_api.py."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from bridge.db import init_platform_db
from nvplan.api.app import create_app
from nvplan.db.session import get_engine, init_db, seed_categories

SUNSET_SLUG = "2026-09-20-sunset-legacy-import"
OTHER_SLUG = "2026-09-19-adopt-decision-provenance"


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    db = tmp_path_factory.mktemp("api-brain") / "api.db"
    engine = init_platform_db(init_db(get_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})))
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        seed_categories(s)
    app = create_app(session_factory=factory)
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def state() -> dict:
    return {}


# --------------------------------------------------------------------------- validate / ingest


def test_01_validate_clean_on_real_tree(client):
    r = client.get("/brain/validate")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["errors"] == []
    assert body["clean"] is True
    # the real tree carries warnings (effect_not_wired etc.) but no error-level findings
    assert isinstance(body["warnings"], list)
    for f in body["warnings"]:
        assert set(f) == {"path", "line", "code", "message", "severity"}
        assert not f["path"].startswith("/")  # relative to the repo root, never absolute


def test_02_ingest_reports_real_file_count_and_is_idempotent(client, state):
    r = client.post("/brain/ingest")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["files_seen"] > 0
    assert body["ingested"] == body["files_seen"]
    assert body["skipped_unchanged"] == 0
    assert body["rejected"] == []
    state["files_seen"] = body["files_seen"]

    r2 = client.post("/brain/ingest")
    body2 = r2.json()
    assert body2["files_seen"] == state["files_seen"]
    assert body2["ingested"] == 0
    assert body2["skipped_unchanged"] == state["files_seen"]


# --------------------------------------------------------------------------- claims list / detail


def test_03_claims_filter_by_kind_and_status(client):
    all_claims = client.get("/brain/claims").json()
    assert len(all_claims) > 0
    for c in all_claims:
        assert set(c) == {"id", "kind", "slug", "title", "status", "date", "path", "has_effect"}

    decisions = client.get("/brain/claims?kind=decision").json()
    assert decisions and all(c["kind"] == "decision" for c in decisions)
    assert {c["slug"] for c in decisions} >= {SUNSET_SLUG, OTHER_SLUG}

    decided = client.get("/brain/claims?status=decided").json()
    assert decided and all(c["status"] == "decided" for c in decided)

    both = client.get("/brain/claims?kind=decision&status=decided").json()
    assert both and all(c["kind"] == "decision" and c["status"] == "decided" for c in both)

    sunset = next(c for c in decisions if c["slug"] == SUNSET_SLUG)
    assert sunset["has_effect"] is True


def test_04_claims_reject_bad_filter_values(client):
    r = client.get("/brain/claims?kind=nonsense")
    assert r.status_code == 400, r.text
    r = client.get("/brain/claims?status=nonsense")
    assert r.status_code == 400, r.text


def test_05_claim_detail_evidence_and_reversal(client, state):
    claims = client.get("/brain/claims?kind=decision").json()
    sunset = next(c for c in claims if c["slug"] == SUNSET_SLUG)
    state["sunset_id"] = sunset["id"]

    r = client.get(f"/brain/claims/{sunset['id']}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["slug"] == SUNSET_SLUG
    assert body["reversal_condition"] and "25%" in body["reversal_condition"]
    assert body["effect"] == {"category_code": "REV", "year": 2027, "value": 22900.0, "unit": "kEUR"}
    assert len(body["evidence"]) >= 2
    tag_kinds = {e["tag_kind"] for e in body["evidence"]}
    assert tag_kinds  # every evidence row carries a tag kind
    for e in body["evidence"]:
        assert set(e) == {"section", "text", "tag_kind", "tag_raw", "target_path", "resolved"}
    assert "links" in body and isinstance(body["links"], list)


def test_06_claim_detail_404_on_missing_id(client):
    assert client.get("/brain/claims/999999").status_code == 404


# --------------------------------------------------------------------------- effects


def test_07_effects_has_the_sunset_decision(client, state):
    r = client.get("/brain/effects")
    assert r.status_code == 200, r.text
    effects = r.json()
    assert len(effects) == 1
    eff = effects[0]
    assert eff["claim_id"] == state["sunset_id"]
    assert eff["decision_slug"] == SUNSET_SLUG
    assert eff["category_code"] == "REV"
    assert eff["year"] == 2027
    assert eff["value"] == pytest.approx(22900.0)
    assert eff["unit"] == "kEUR"
    assert eff["decided_on"] == "2026-09-20"


# --------------------------------------------------------------------------- bridge apply / impact


def test_08_bridge_apply_before_a_baseline_plan_needs_actuals(client):
    """No actuals ingested yet in this database: applying effects still finds the one
    decided effect, but running the plan on top of it fails cleanly (no actuals -> the
    existing ValueError -> 400 path), not a 500."""
    r = client.post("/bridge/apply")
    assert r.status_code == 400, r.text


def test_09_bridge_apply_moves_the_plan(client, state):
    assert client.post("/ingest").status_code == 200
    assert client.post("/plan/run", json={"created_by": "brain-test"}).status_code == 200

    before = client.get("/plan/grid?scenario_kind=base").json()
    before_2027 = before["rows"][0]["cells"]["2027"]
    assert before_2027["path"] == "valorized"

    r = client.post("/bridge/apply", json={"created_by": "brain-test"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["message"] is None
    assert body["applied"] == [2027]
    assert body["shadowed"] == []
    assert body["plan_run"] is not None
    assert body["plan_run"]["n_plan_values"] > 0

    after = client.get("/plan/grid?scenario_kind=base").json()
    after_2027 = after["rows"][0]["cells"]["2027"]
    assert after_2027["path"] == "decided"
    assert after_2027["value"] == pytest.approx(22900.0)
    state["default_2027"] = before_2027["value"]


def test_10_bridge_apply_reports_cleanly_when_nothing_decided(tmp_path):
    """A fresh, isolated database with actuals and a baseline plan but the brain never
    ingested: decided_effects is empty, so bridge/apply must not run a pointless plan and
    must not treat that as an error."""
    db = tmp_path / "no-brain.db"
    engine = init_platform_db(init_db(get_engine(f"sqlite:///{db}")))
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        seed_categories(s)
    app = create_app(session_factory=factory)
    with TestClient(app) as c:
        assert c.post("/ingest").status_code == 200
        assert c.post("/plan/run", json={"created_by": "brain-test"}).status_code == 200

        r = c.post("/bridge/apply", json={"created_by": "brain-test"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["plan_run"] is None
        assert body["applied"] == []
        assert body["shadowed"] == []
        assert body["message"] and "no decided effect" in body["message"]


def test_11_impact_for_sunset_decision_returns_displaced_default(client, state):
    r = client.get(f"/brain/claims/{state['sunset_id']}/impact")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) >= 1
    for row in rows:
        assert set(row) == {
            "plan_value_id", "scenario_kind", "category_code", "year", "value", "path", "displaced_default",
        }
        assert row["category_code"] == "REV"
        assert row["year"] == 2027
        assert row["path"] == "decided"

    base_row = next(row for row in rows if row["scenario_kind"] == "base")
    assert base_row["value"] == pytest.approx(22900.0)
    assert base_row["displaced_default"] == pytest.approx(state["default_2027"])


def test_12_impact_empty_for_a_claim_that_drove_nothing(client, state):
    claims = client.get("/brain/claims?kind=decision").json()
    other = next(c for c in claims if c["slug"] == OTHER_SLUG)
    r = client.get(f"/brain/claims/{other['id']}/impact")
    assert r.status_code == 200, r.text
    assert r.json() == []


# --------------------------------------------------------------------------- root / static


def test_13_root_serves_index_and_static_is_mounted(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    r = client.get("/static/index.html")
    assert r.status_code == 200


# --------------------------------------------------------------------------- backward compatibility


def test_14_existing_finance_only_database_still_serves_every_route(tmp_path):
    """A database file created before the brain tables existed (nvplan metadata only)
    must keep serving every pre-existing route once opened through create_app - and now
    also gains the brain tables (idempotent create_all), so /brain/claims works too."""
    db_path = tmp_path / "finance_only.db"
    engine = init_db(get_engine(f"sqlite:///{db_path}"))
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        seed_categories(s)
    # engine/connection above is done with; create_app opens its own via make_session_factory.

    app = create_app(db_url=f"sqlite:///{db_path}")
    with TestClient(app) as c:
        h = c.get("/health").json()
        assert h["status"] == "ok" and h["n_actuals"] == 0

        assert c.post("/ingest").status_code == 200
        assert c.post("/plan/run", json={"created_by": "compat-test"}).status_code == 200
        assert c.get("/plan/grid").status_code == 200

        # the brain tables now exist (created on this boot) even though the file predates them
        assert c.get("/brain/claims").json() == []
        assert c.get("/brain/validate").status_code == 200
