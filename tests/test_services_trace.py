"""trace: click a number, get its full lineage."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from nvplan.config import DATA_DIR
from nvplan.db.models import Parameter, Statement, StatementLine
from nvplan.db.session import SessionLocal, get_engine, init_db, seed_categories
from nvplan.ingest.actuals import ingest_actuals, load_actuals_csv
from nvplan.services.planning import run_plan
from nvplan.services.trace import (
    TraceNode,
    find_plan_value,
    find_statement_line,
    render_trace,
    trace_derivation,
    trace_plan_value,
    trace_statement_line,
)


@pytest.fixture(scope="module")
def session():
    engine = init_db(get_engine("sqlite://"))
    with SessionLocal(bind=engine) as s:
        seed_categories(s)
        ingest_actuals(s, load_actuals_csv(DATA_DIR / "actuals.csv"))
        yield s


@pytest.fixture(scope="module")
def run(session):
    return run_plan(session, created_by="tester")


@pytest.fixture(scope="module")
def pers_tree(session, run) -> TraceNode:
    pv = find_plan_value(session, scenario_kind="base", category_code="PERS", year=2028)
    return trace_plan_value(session, pv.id)


def test_pers_2028_base_tree(session, run, pers_tree):
    root = pers_tree
    assert root.kind == "plan_value" and root.label == "Personnel costs · 2028 · Base"
    assert root.path == "cascaded" and root.key == "plan:base:PERS:2028"
    assert root.formula_text == "alpha*(1+v)^(t-t0) + beta*PlanRevenue_t"
    assert root.ai is None
    # parameters equal the parameter row (both on the root and on the param child)
    prm = session.get(Parameter, run.parameter_ids["PERS"])
    assert root.parameters["alpha"] == prm.alpha and root.parameters["beta"] == prm.beta
    assert root.parameters["v"] == prm.valorization_rate
    param_node = root.find(key="param:PERS")
    assert param_node is not None and param_node.kind == "parameter" and param_node.ref_id == prm.id
    assert param_node.parameters["alpha"] == prm.alpha and param_node.parameters["beta"] == prm.beta
    assert param_node.parameters["v"] == prm.valorization_rate and param_node.parameters["r_squared"] == prm.r_squared
    assert len(param_node.inputs["points"]) == 5  # regression points shown
    # input node: REV 2028 base plan value ...
    rev = root.find(key="plan:base:REV:2028")
    assert rev is not None and rev.kind == "plan_value" and rev.path == "valorized"
    assert rev.meta["category_code"] == "REV" and rev.meta["year"] == 2028
    assert root.inputs["PlanRevenue_t"] == rev.value
    # ... whose own subtree reaches param:REV and the ILLUSTRATIVE anchor actual
    g = rev.find(key="param:REV")
    assert g is not None and g.kind == "parameter" and "g" in g.parameters
    anchor = rev.find(key="actual:REV:2025")
    assert anchor is not None and anchor.kind == "actual" and anchor.source_label == "ILLUSTRATIVE"
    assert anchor.value == pytest.approx(20766.0, abs=1.0)
    assert anchor.children == []
    # root value reproduces from the tree's own parameters and inputs
    p = root.parameters
    assert root.value == pytest.approx(p["alpha"] * (1 + p["v"]) ** (2028 - 2025) + p["beta"] * rev.value, rel=1e-12)


def test_render_pers(pers_tree):
    text = render_trace(pers_tree)
    print("\n" + text)
    assert text.startswith("Personnel costs · 2028 · Base")
    assert "k€" in text
    assert "path: cascaded" in text
    assert "formula: alpha*(1+v)^(t-t0) + beta*PlanRevenue_t" in text
    assert "PlanRevenue" in text and "ILLUSTRATIVE" in text
    assert "R²=" in text and "alpha=" in text and "beta=" in text
    assert "regression points (5)" in text
    assert "source data:" in text


def test_trace_bs_cash_reaches_cf_and_plan_values(session, run):
    sl = find_statement_line(session, scenario_kind="base", statement="bs", line_code="cash", year=2028)
    tree = trace_statement_line(session, sl.id, max_depth=20)
    assert tree.kind == "statement_line" and tree.label == "BS cash · 2028 · Base"
    closing = tree.find(key="stmt:base:cf:closing_cash:2028")
    assert closing is not None and closing.kind == "statement_line" and closing.meta["line_code"] == "closing_cash"
    ni = closing.find(key="stmt:base:cf:net_income:2028")
    assert ni is not None and ni.kind == "statement_line"
    pl_ni = ni.find(key="stmt:base:pl:net_income:2028")
    assert pl_ni is not None and pl_ni.meta["statement"] == "pl"
    kinds = {n.key: n.kind for n in tree.walk()}
    assert kinds.get("plan:base:REV:2028") == "plan_value"
    assert kinds.get("plan:base:PERS:2028") == "plan_value"
    assert kinds.get("plan:base:DEPR:2028") == "plan_value"
    assert kinds.get("stmt:base:bs:cash:2025") == "statement_line"  # opening balance sheet reached
    text = render_trace(tree)
    assert "BS cash · 2028 · Base" in text and "closing_cash" in text and "opening_balance_sheet" not in text[:200]


def test_depth_guard_and_refs(session, run):
    sl = find_statement_line(session, scenario_kind="base", statement="bs", line_code="cash", year=2030)
    shallow = trace_statement_line(session, sl.id, max_depth=3)
    assert max(n.depth for n in shallow.walk()) == 3
    assert any(n.truncated for n in shallow.walk())
    deep = trace_statement_line(session, sl.id, max_depth=40)
    # shared inputs are expanded once and referenced afterwards
    param_nodes = [n for n in deep.walk() if n.key == "param:PERS"]
    assert len(param_nodes) >= 1 and sum(1 for n in param_nodes if not n.ref) == 1
    # every derivation id in the tree exists and no node repeats along its own ancestry
    assert not any("cycle" in n.label for n in deep.walk())


def test_trace_derivation_direct_and_errors(session, run):
    node = trace_derivation(session, run.derivation_ids["param:MAT"])
    assert node.kind == "parameter" and node.ref_id == run.parameter_ids["MAT"]
    assert node.children == []
    with pytest.raises(LookupError):
        trace_derivation(session, 10**9)
    with pytest.raises(LookupError):
        trace_plan_value(session, 10**9)
    with pytest.raises(LookupError):
        find_plan_value(session, scenario_kind="base", category_code="REV", year=1999)


def test_find_plan_value_uses_latest_scenario(session, run):
    pv_old = find_plan_value(session, scenario_kind="worst", category_code="MAT", year=2026)
    run2 = run_plan(session, created_by="tester", label_suffix=" again")
    pv_new = find_plan_value(session, scenario_kind="worst", category_code="MAT", year=2026)
    assert pv_new.scenario_id == run2.scenario_ids["worst"] != pv_old.scenario_id
    assert pv_new.value == pv_old.value
    pv_pinned = find_plan_value(session, scenario_kind="worst", category_code="MAT", year=2026,
                                scenario_id=run.scenario_ids["worst"])
    assert pv_pinned.id == pv_old.id
    d = trace_plan_value(session, pv_new.id).to_dict()
    assert d["kind"] == "plan_value" and d["children"]


def test_every_statement_line_traces(session, run):
    for sl in session.scalars(select(StatementLine).where(StatementLine.scenario_id == run.scenario_ids["best"],
                                                          StatementLine.statement == Statement.cf)).all():
        tree = trace_statement_line(session, sl.id, max_depth=6)
        assert tree.kind == "statement_line" and tree.value == sl.value
