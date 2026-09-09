"""API end-to-end with TestClient on a file-based SQLite and injected fake models."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from nvplan.ai.fake import scripted_deviation_model, scripted_env_scan_model, scripted_revenue_model
from nvplan.api.app import create_app
from nvplan.db.session import get_engine, init_db, seed_categories

CODES = ["REV", "MAT", "EXT", "PERS", "OTH", "DEPR"]
YEARS = [2026, 2027, 2028, 2029, 2030]


def fake_model_factory(touchpoint: str, ctx: dict):
    if touchpoint == "env_scan":
        return scripted_env_scan_model()
    if touchpoint == "revenue_proposal":
        default = ctx["default_value"]
        return scripted_revenue_model(
            year=ctx["year"], proposed_value=round(default * 0.95, 1),
            rationale=f"client contract ends Q2 {ctx['year']}: {default:,.1f} * 0.95", cited_note_ids=ctx["note_ids"][:1],
        )
    if touchpoint == "deviation_explanation":
        return scripted_deviation_model(scenario_kind=ctx["scenario_kind"], year=ctx["year"], summary="scripted", contributions=[])
    raise AssertionError(touchpoint)


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    db = tmp_path_factory.mktemp("api") / "api.db"
    engine = init_db(get_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False}))
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        seed_categories(s)
    app = create_app(session_factory=factory)
    app.state.model_factory = fake_model_factory
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def state() -> dict:
    return {}


def test_01_health_empty(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok" and r.json()["n_actuals"] == 0


def test_02_ingest(client):
    r = client.post("/ingest")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rows"] == 60 and body["source_label"] == "ILLUSTRATIVE" and body["illustrative"] is True
    assert body["notes_seeded"] == 4
    assert client.get("/plan/grid").status_code == 404  # no plan yet


def test_03_ingest_upload_csv(client):
    csv = "category_code,year,value\nREV,2025,20766.0\n"
    r = client.post("/ingest?source_label=UPLOAD-TEST&with_notes=false", content=csv, headers={"content-type": "text/csv"})
    assert r.status_code == 200 and r.json()["rows"] == 1 and r.json()["source_label"] == "UPLOAD-TEST"
    # restore the illustrative row so the rest of the module sees the generated data
    r = client.post("/ingest")
    assert r.status_code == 200 and r.json()["notes_seeded"] == 0


def test_04_plan_run_and_grid(client, state):
    r = client.post("/plan/run", json={"created_by": "api-test"})
    assert r.status_code == 200, r.text
    run = r.json()
    assert set(run["scenario_ids"]) == {"base", "best", "worst"} and run["n_plan_values"] == 90
    state["run1"] = run

    g = client.get("/plan/grid?scenario_kind=base").json()
    assert g["scenario_id"] == run["scenario_ids"]["base"] and g["illustrative"] is True
    assert g["years"] == YEARS and [r["category_code"] for r in g["rows"]] == CODES
    for row in g["rows"]:
        assert set(map(int, row["cells"])) == set(YEARS)
        for cell in row["cells"].values():
            assert cell["plan_value_id"] > 0 and cell["ai_record_id"] is None
            assert cell["path"] == ("valorized" if row["category_code"] == "REV" else "cascaded")
    params = {p["category_code"]: p for p in g["parameters"]}
    assert set(params) == {"REV", "MAT", "EXT", "PERS", "OTH"}
    assert params["REV"]["growth_rate"] > 0
    for code in ("MAT", "EXT", "PERS", "OTH"):
        assert params[code]["r_squared"] > 0.9 and params[code]["parameter_id"] == run["parameter_ids"][code]
        assert params[code]["alpha"] is not None and params[code]["beta"] > 0
    state["grid1"] = g

    scen = client.get("/plan/scenarios").json()
    assert len(scen) == 3 and {s["kind"] for s in scen} == {"base", "best", "worst"}
    best = client.get("/plan/grid?scenario_kind=best").json()
    assert best["rows"][0]["cells"]["2027"]["value"] == pytest.approx(1.08 * g["rows"][0]["cells"]["2027"]["value"])
    assert client.get("/plan/grid?scenario_kind=nope").status_code == 404
    assert client.get(f"/plan/grid?scenario_kind=best&scenario_id={run['scenario_ids']['base']}").status_code == 404


def test_05_trace_pers_2028(client, state):
    pers = next(r for r in state["grid1"]["rows"] if r["category_code"] == "PERS")
    pv_id = pers["cells"]["2028"]["plan_value_id"]
    r = client.get(f"/trace/plan-value/{pv_id}?format=text")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert r.text.startswith("Personnel costs · 2028 · Base")
    assert "ILLUSTRATIVE" in r.text and "PlanRevenue" in r.text and "R²=" in r.text
    j = client.get(f"/trace/plan-value/{pv_id}").json()
    assert j["kind"] == "plan_value" and j["key"] == "plan:base:PERS:2028" and j["children"]
    assert client.get("/trace/plan-value/999999").status_code == 404


def test_06_statements_and_trace(client, state):
    bs = client.get("/statements/base?statement=bs").json()
    assert bs["statement"] == "bs" and bs["years"] == [2025, *YEARS] and bs["consistency"] == []
    cash = next(r for r in bs["rows"] if r["line_code"] == "cash")
    sl_id = cash["cells"]["2028"]["statement_line_id"]
    t = client.get(f"/trace/statement-line/{sl_id}").json()
    assert t["kind"] == "statement_line" and t["label"] == "BS cash · 2028 · Base"
    assert "closing_cash" in client.get(f"/trace/statement-line/{sl_id}?format=text").text
    pl = client.get("/statements/base?statement=pl").json()
    assert [r["line_code"] for r in pl["rows"]][:2] == ["revenue", "material"] and pl["years"] == YEARS
    assert client.get("/statements/base?statement=xx").status_code == 422
    assert client.get("/statements/base?statement=cf").json()["years"] == YEARS


def test_07_env_scan_and_revenue_proposal(client, state):
    r = client.post("/ai/env-scan", json={"positions_subset": ["D1", "D2"]})
    assert r.status_code == 200, r.text
    scan = r.json()
    assert scan["touchpoint"] == "env_scan" and scan["status"] == "proposed" and len(scan["note_ids"]) == 2
    assert "---USER---" in scan["prompt_text"] and scan["model_version"] == "fake"

    r = client.post("/ai/revenue-proposal", json={"scenario_kind": "base", "year": 2027})
    assert r.status_code == 200, r.text
    body = r.json()
    default = state["grid1"]["rows"][0]["cells"]["2027"]["value"]
    assert body["default_value"] == pytest.approx(default)
    rec = body["record"]
    assert rec["touchpoint"] == "revenue_proposal" and rec["status"] == "proposed"
    assert rec["proposed_value"] == pytest.approx(round(default * 0.95, 1))
    assert rec["year"] == 2027 and rec["category_code"] == "REV" and rec["confirmed_by"] is None
    assert "Propose" in rec["prompt_text"] or "revenue" in rec["prompt_text"].lower()
    state["proposal"] = rec

    listed = client.get("/ai/records?touchpoint=revenue_proposal&status=proposed").json()
    assert [x["id"] for x in listed] == [rec["id"]]
    assert client.get(f"/ai/records/{rec['id']}").json()["prompt_text"] == rec["prompt_text"]
    assert client.get("/ai/records/999").status_code == 404
    # nothing entered the plan
    g = client.get("/plan/grid").json()
    assert g["scenario_id"] == state["run1"]["scenario_ids"]["base"]
    assert g["rows"][0]["cells"]["2027"]["path"] == "valorized"


def test_08_confirm_reruns(client, state):
    rec = state["proposal"]
    r = client.post(f"/ai/records/{rec['id']}/confirm", json={"confirmed_by": "C. Andres"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["record"]["status"] == "confirmed" and body["record"]["confirmed_by"] == "C. Andres"
    run2 = body["run"]
    assert run2["scenario_ids"]["base"] != state["run1"]["scenario_ids"]["base"]

    g = client.get("/plan/grid").json()
    assert g["scenario_id"] == run2["scenario_ids"]["base"]
    cell = g["rows"][0]["cells"]["2027"]
    assert cell["value"] == pytest.approx(rec["proposed_value"]) and cell["path"] == "ai_proposed"
    assert cell["ai_record_id"] == rec["id"]
    assert g["rows"][0]["cells"]["2026"]["path"] == "valorized"
    pers = next(r for r in g["rows"] if r["category_code"] == "PERS")["cells"]["2027"]
    text = client.get(f"/trace/plan-value/{pers['plan_value_id']}?format=text").text
    assert "confirmed_by=C. Andres" in text and "ai_proposed" in text and "prompt (verbatim)" in text
    assert len(client.get("/plan/scenarios").json()) == 6
    # a second proposal for the same year is judged against the same default
    assert client.post("/ai/revenue-proposal", json={"scenario_kind": "base", "year": 2027}).json()["default_value"] == \
        pytest.approx(state["grid1"]["rows"][0]["cells"]["2027"]["value"])


def test_09_reject_confirmed_is_409(client, state):
    rec = state["proposal"]
    r = client.post(f"/ai/records/{rec['id']}/reject", json={"rejected_by": "bob"})
    assert r.status_code == 409 and "confirmed" in r.json()["detail"]
    r = client.post(f"/ai/records/{rec['id']}/confirm", json={"confirmed_by": "bob"})
    assert r.status_code == 409
    assert client.post("/ai/records/999/confirm", json={"confirmed_by": "bob"}).status_code == 404
    # a fresh proposal can be rejected and leaves no trace in figures
    new = client.post("/ai/revenue-proposal", json={"scenario_kind": "base", "year": 2028}).json()["record"]
    r = client.post(f"/ai/records/{new['id']}/reject", json={"rejected_by": "bob"})
    assert r.status_code == 200 and r.json()["status"] == "rejected" and r.json()["confirmed_by"] == "bob"
    assert len(client.get("/plan/scenarios").json()) == 6


def test_10_backtest_and_deviation(client):
    r = client.get("/backtest")
    assert r.status_code == 200, r.text
    bt = r.json()
    assert len(bt["summary"]) == 15 and bt["n_cases"] == 12 and bt["illustrative"] is True
    assert {row["verdict"] for row in bt["summary"]} <= {"within", "marginal", "missed"}
    assert bt["markdown"].startswith("## Backtest") and bt["thresholds"] == {"mape_within": 0.05, "mape_marginal": 0.08}
    assert len(bt["summary_default_path"]) == 12

    d = client.get("/deviation?scenario_kind=base").json()
    assert d["rows"] == [] and d["years"] == []  # plan years 2026+ have no actuals

    y = client.get("/deviation/backtest-year?year=2025").json()
    assert y["train_window"] == [2020, 2024]
    rev = [r for r in y["rows"] if r["category_code"] == "REV"]
    assert len(rev) == 1 and rev[0]["basis"] == "default_revenue" and rev[0]["actual"] == pytest.approx(20766.0, abs=1)
    assert rev[0]["deviation_plan_minus_actual"] == pytest.approx(rev[0]["plan"] - rev[0]["actual"])
    assert client.get("/deviation/backtest-year?year=2018").status_code == 404


def test_11_deviation_explanation(client):
    r = client.post("/ai/deviation-explanation", json={"scenario_kind": "base", "year": 2025})
    assert r.status_code == 200, r.text
    assert r.json()["touchpoint"] == "deviation_explanation" and r.json()["proposed_value"] is None


def test_12_no_model_no_key_is_503(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client.app.state.model_factory = None
    try:
        r = client.post("/ai/env-scan", json={})
        assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["detail"]
        r = client.post("/ai/revenue-proposal", json={"scenario_kind": "base", "year": 2029})
        assert r.status_code == 503
    finally:
        client.app.state.model_factory = fake_model_factory


def test_create_app_with_db_url(tmp_path):
    app = create_app(db_url=f"sqlite:///{tmp_path / 'x.db'}")
    with TestClient(app) as c:
        h = c.get("/health").json()
        assert h["n_actuals"] == 0 and h["db_url"].endswith("x.db")
