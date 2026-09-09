"""Projection, scenarios, ledger lineage and formula integrity."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from nvplan.config import DATA_DIR, PLAN_YEARS, REGRESSION_WINDOW
from nvplan.core import Derivation, DerivationLedger, jsonable, to_wide
from nvplan.core.depreciation import depreciation_schedule
from nvplan.core.projector import (
    DEFAULT_SPREAD,
    PLAN_COLUMNS,
    project_all,
    project_scenario,
    scenario_revenue_paths,
)
from nvplan.core.regression import fit_all, revenue_default_path

COST_CODES = ("MAT", "EXT", "PERS", "OTH")
PLAN_YEAR_LIST = list(range(PLAN_YEARS[0], PLAN_YEARS[1] + 1))


def run_pipeline(wide: pd.DataFrame, base_revenue: pd.Series | None = None):
    """Full core run on one fresh ledger. Returns (ledger, fits, sched, base_rev, plan)."""
    ledger = DerivationLedger()
    fits = fit_all(wide, COST_CODES, REGRESSION_WINDOW, ledger=ledger, depreciation=wide["DEPR"])
    sched = depreciation_schedule(pd.read_csv(DATA_DIR / "investment_plan.csv"), ledger=ledger)
    depr = sched.set_index("year")["depreciation"]
    base = revenue_default_path(wide, REGRESSION_WINDOW, PLAN_YEARS, ledger=ledger)
    if base_revenue is not None:
        base = base_revenue
    plan = project_all(fits, base, DEFAULT_SPREAD, t0=REGRESSION_WINDOW[1], depreciation=depr, ledger=ledger)
    return ledger, fits, sched, base, plan


@pytest.fixture(scope="module")
def wide() -> pd.DataFrame:
    return to_wide(pd.read_csv(DATA_DIR / "actuals.csv"))


@pytest.fixture(scope="module")
def pipeline(wide):
    return run_pipeline(wide)


def cell(plan: pd.DataFrame, scenario: str, code: str, year: int) -> float:
    sel = plan[(plan.scenario == scenario) & (plan.category_code == code) & (plan.year == year)]
    assert len(sel) == 1, (scenario, code, year, len(sel))
    return float(sel["value"].iloc[0])


# --------------------------------------------------------------------------- shape


def test_plan_frame_shape(pipeline):
    _, _, _, _, plan = pipeline
    assert list(plan.columns) == ["scenario"] + PLAN_COLUMNS
    assert set(plan.scenario) == {"base", "best", "worst"}
    assert set(plan.category_code) == {"REV", "MAT", "EXT", "PERS", "OTH", "DEPR"}
    assert sorted(plan.year.unique()) == PLAN_YEAR_LIST
    assert len(plan) == 3 * 6 * 5
    assert not plan.duplicated(["scenario", "category_code", "year"]).any()
    assert set(plan[plan.category_code == "REV"].path) == {"valorized"}
    assert set(plan[plan.category_code != "REV"].path) == {"cascaded"}


# --------------------------------------------------------------------------- 5. hand computation


def test_pers_2028_base_by_hand(pipeline):
    _, fits, _, base, plan = pipeline
    f = fits["PERS"]
    t0 = REGRESSION_WINDOW[1]
    expected = f.alpha * (1 + f.valorization_rate) ** (2028 - t0) + f.beta * base.loc[2028]
    assert cell(plan, "base", "PERS", 2028) == expected  # exact: same arithmetic
    # and fully by hand from the raw window data (independent of the FitResult object)
    years = list(range(2021, 2026))
    wide = to_wide(pd.read_csv(DATA_DIR / "actuals.csv"))
    rev = wide.loc[years, "REV"].to_numpy(float)
    cost = wide.loc[years, "PERS"].to_numpy(float)
    slope, intercept = np.polyfit(rev, cost, 1)
    fixed = cost - slope * rev
    v = np.mean(fixed[1:] / fixed[:-1] - 1)
    g = np.mean(rev[1:] / rev[:-1] - 1)
    plan_rev_2028 = wide.loc[2025, "REV"] * (1 + g) ** 3
    assert base.loc[2028] == pytest.approx(plan_rev_2028, rel=1e-12)
    hand = intercept * (1 + v) ** 3 + slope * plan_rev_2028
    assert cell(plan, "base", "PERS", 2028) == pytest.approx(hand, rel=1e-9)


def test_oth_equals_regressed_part_plus_depreciation(pipeline):
    _, fits, sched, base, plan = pipeline
    f = fits["OTH"]
    depr = sched.set_index("year")["depreciation"]
    for scenario in ("base", "best", "worst"):
        for y in PLAN_YEAR_LIST:
            rev_y = cell(plan, scenario, "REV", y)
            regressed = f.alpha * (1 + f.valorization_rate) ** (y - 2025) + f.beta * rev_y
            assert cell(plan, scenario, "OTH", y) == pytest.approx(regressed + depr.loc[y], abs=1e-9)
            assert cell(plan, scenario, "DEPR", y) == depr.loc[y]


def test_scenario_revenue_spread(pipeline):
    _, _, _, base, plan = pipeline
    for y in PLAN_YEAR_LIST:
        assert cell(plan, "base", "REV", y) == base.loc[y]
        assert cell(plan, "best", "REV", y) == pytest.approx(1.08 * base.loc[y], rel=1e-9)
        assert cell(plan, "worst", "REV", y) == pytest.approx(0.92 * base.loc[y], rel=1e-9)
    # costs follow the same engine: best - base == beta * (best_rev - base_rev)
    _, fits, _, _, _ = pipeline
    for code in COST_CODES:
        for y in PLAN_YEAR_LIST:
            d_rev = cell(plan, "best", "REV", y) - cell(plan, "base", "REV", y)
            d_cost = cell(plan, "best", code, y) - cell(plan, "base", code, y)
            assert d_cost == pytest.approx(fits[code].beta * d_rev, abs=1e-8)


# --------------------------------------------------------------------------- lineage


def test_every_plan_row_has_a_resolvable_derivation(pipeline):
    ledger, fits, _, _, plan = pipeline
    ledger.validate()
    for row in plan.itertuples(index=False):
        assert row.derivation_key in ledger, row
        d = ledger.get(row.derivation_key)
        for p in d.parents:
            assert p in ledger, (row.derivation_key, p)
        json.dumps(d.inputs)
        json.dumps(d.parameters)
    # cost derivations: parents = (param, revenue key[, depr key]) and the recorded inputs match
    for row in plan[plan.category_code.isin(COST_CODES)].itertuples(index=False):
        d = ledger.get(row.derivation_key)
        assert row.derivation_key == f"plan:{row.scenario}:{row.category_code}:{row.year}"
        assert d.parents[0] == f"param:{row.category_code}"
        assert d.parents[1] == f"plan:{row.scenario}:REV:{row.year}"
        assert d.inputs["PlanRevenue_t"] == cell(plan, row.scenario, "REV", row.year)
        assert d.parameters["alpha"] == fits[row.category_code].alpha
        assert d.parameters["t0"] == 2025 and d.parameters["t"] == row.year
        if row.category_code == "OTH":
            assert d.parents[2] == f"depr:{row.year}"
            assert d.formula_text == "alpha*(1+v)^(t-t0) + beta*PlanRevenue_t + DEPR_t"
        else:
            assert len(d.parents) == 2
            assert d.formula_text == "alpha*(1+v)^(t-t0) + beta*PlanRevenue_t"
    # scenario revenue keys chain back to the default path and the actual
    assert ledger.get("plan:best:REV:2027").parents == ("plan:base:REV:2027",)
    assert ledger.get("plan:base:REV:2027").parents == ("plan:default:REV:2027",)
    anc = ledger.ancestors("plan:best:OTH:2027")
    assert "actual:REV:2025" in anc and "param:OTH" in anc and "capex:2027" in anc


def test_topo_order(pipeline):
    ledger, *_ = pipeline
    order = ledger.topo_order()
    assert len(order) == len(ledger) == len(set(order))
    pos = {k: i for i, k in enumerate(order)}
    for d in ledger:
        for p in d.parents:
            assert pos[p] < pos[d.key], (p, d.key)
    assert [d.key for d in ledger][0] == "param:MAT"  # insertion order preserved by __iter__
    recs = ledger.records()
    assert [r["key"] for r in recs] == order
    assert set(recs[0]) == {"key", "formula_text", "inputs_json", "parameters_json", "parent_keys"}


def test_ledger_basics():
    ledger = DerivationLedger()
    d = ledger.add("a", "x", inputs={"n": np.int64(3), "v": np.float64(1.5), "arr": np.array([1.0, 2.0])})
    assert isinstance(d, Derivation)
    assert d.inputs == {"n": 3, "v": 1.5, "arr": [1.0, 2.0]}
    assert type(d.inputs["n"]) is int and type(d.inputs["v"]) is float
    with pytest.raises(KeyError):
        ledger.add("a", "again")
    ledger.add("a", "replaced", replace=True)
    assert ledger.get("a").formula_text == "replaced"
    ledger.add("b", "y", parents=("a",))
    ledger.add("c", "z", parents=("missing",))
    assert ledger.missing_parents() == {"c": ["missing"]}
    with pytest.raises(KeyError):
        ledger.validate()
    with pytest.raises(KeyError):
        ledger.topo_order()
    assert jsonable(pd.Series({2026: 1.0})) == {"2026": 1.0}
    with pytest.raises(TypeError):
        jsonable(object())
    # cycles are rejected
    cyc = DerivationLedger()
    cyc.add("p", "", parents=("q",))
    cyc.add("q", "", parents=("p",))
    with pytest.raises(ValueError):
        cyc.topo_order()


def test_project_scenario_direct_and_custom_label(wide):
    ledger = DerivationLedger()
    fits = fit_all(wide, ledger=ledger)
    sched = depreciation_schedule(pd.read_csv(DATA_DIR / "investment_plan.csv"), ledger=ledger)
    depr = sched.set_index("year")["depreciation"]
    path = pd.Series({2026: 21000.0, 2027: 22000.0})
    keys = {}
    for y, val in path.items():
        keys[y] = f"ai:REV:{y}"
        ledger.add(keys[y], "AI proposed revenue (confirmed)", inputs={"value": val})
    df = project_scenario(fits, path, t0=2025, scenario="base", depreciation=depr, ledger=ledger,
                          revenue_path_keys=keys, revenue_path_label="ai_proposed")
    assert set(df[df.category_code == "REV"].path) == {"ai_proposed"}
    assert set(df[df.category_code == "REV"].derivation_key) == {"ai:REV:2026", "ai:REV:2027"}
    assert ledger.get("plan:base:MAT:2027").parents == ("param:MAT", "ai:REV:2027")
    with pytest.raises(ValueError):
        project_scenario(fits, path, t0=2025, scenario="x", depreciation=depr, ledger=DerivationLedger(),
                         revenue_path_keys={2026: "k"})


def test_scenario_revenue_paths_without_default_keys():
    ledger = DerivationLedger()
    base = pd.Series({2026: 100.0, 2027: 200.0})
    paths = scenario_revenue_paths(base, {"best": 0.1, "worst": -0.1}, ledger=ledger)
    assert paths["best"].tolist() == pytest.approx([110.0, 220.0])
    assert paths["worst"].tolist() == pytest.approx([90.0, 180.0])
    assert ledger.get("plan:base:REV:2026").parents == ("input:REV:2026",)
    assert ledger.get("plan:worst:REV:2027").parameters["spread"] == -0.1
    ledger.validate()
    with pytest.raises(ValueError):
        scenario_revenue_paths(base, {"base": 0.0}, ledger=DerivationLedger())


# --------------------------------------------------------------------------- 6. formula integrity


def test_bumping_2027_revenue_moves_costs_by_beta_only_in_2027(wide):
    _, fits, _, base, plan0 = run_pipeline(wide)
    bumped = base.copy()
    bumped.loc[2027] += 1000.0
    _, fits1, _, _, plan1 = run_pipeline(wide, base_revenue=bumped)
    for code in COST_CODES:
        assert fits1[code].beta == fits[code].beta  # parameters untouched

    spread = {"base": 0.0, **DEFAULT_SPREAD}
    for scenario, s in spread.items():
        for y in PLAN_YEAR_LIST:
            d_rev = cell(plan1, scenario, "REV", y) - cell(plan0, scenario, "REV", y)
            expected_rev = 1000.0 * (1 + s) if y == 2027 else 0.0
            assert d_rev == pytest.approx(expected_rev, abs=1e-9)
            for code in COST_CODES:
                d_cost = cell(plan1, scenario, code, y) - cell(plan0, scenario, code, y)
                assert d_cost == pytest.approx(fits[code].beta * expected_rev, abs=1e-9), (scenario, code, y)
            assert cell(plan1, scenario, "DEPR", y) == cell(plan0, scenario, "DEPR", y)


def test_no_hard_coded_results_scaling_actuals(wide):
    """Scaling every actual by 2 scales every plan value by 2 (homogeneity of the engine)."""
    _, _, _, _, plan0 = run_pipeline(wide)
    _, _, _, _, plan2 = run_pipeline(wide * 2.0)
    m = plan0.merge(plan2, on=["scenario", "category_code", "year"], suffixes=("", "_2"))
    # DEPR comes from the (unscaled) investment plan, so compare OTH net of it
    depr = m[m.category_code == "DEPR"].set_index(["scenario", "year"])["value"]
    is_oth = m.category_code == "OTH"
    idx = pd.MultiIndex.from_frame(m.loc[is_oth, ["scenario", "year"]])
    m.loc[is_oth, "value"] -= depr.reindex(idx).to_numpy()
    m.loc[is_oth, "value_2"] -= depr.reindex(idx).to_numpy()
    rows = m[m.category_code != "DEPR"]
    assert (rows["value_2"] / rows["value"]).to_numpy() == pytest.approx(2.0, rel=1e-9)
