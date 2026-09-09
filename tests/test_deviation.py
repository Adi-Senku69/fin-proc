"""Deviation plan vs actual: exact arithmetic, derivations with resolvable parents, decomposition."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from nvplan.core import DerivationLedger
from nvplan.core.deviation import (
    DEVIATION_COLUMNS,
    DeviationRow,
    compute_deviation,
    decompose_cost_deviation,
    dev_key,
    devx_key,
)
from nvplan.core.regression import FitResult, actual_key


def _plan_frame(ledger: DerivationLedger, rows: list[tuple[str, int, float]], scenario="base") -> pd.DataFrame:
    """Hand-built plan long frame; registers a plan derivation per row so parents resolve."""
    out = []
    for code, year, value in rows:
        key = f"plan:{scenario}:{code}:{year}"
        ledger.add(key, "hand-built plan value", inputs={"value": value})
        out.append({"scenario": scenario, "category_code": code, "year": year, "value": value, "derivation_key": key})
    return pd.DataFrame(out)


def _actual_frame(rows: list[tuple[str, int, float]]) -> pd.DataFrame:
    return pd.DataFrame([{"category_code": c, "year": y, "value": v} for c, y, v in rows])


# --------------------------------------------------------------------------- arithmetic


def test_compute_deviation_exact_arithmetic():
    ledger = DerivationLedger()
    plan = _plan_frame(ledger, [("REV", 2026, 20000.0), ("MAT", 2026, 1200.0), ("PERS", 2026, 9000.0),
                                ("MAT", 2027, 1250.0)])
    actual = _actual_frame([("REV", 2026, 21000.0), ("MAT", 2026, 1150.0), ("PERS", 2026, 9000.0),
                            ("EXT", 2026, 800.0)])  # EXT has no plan -> not compared
    dev = compute_deviation(plan, actual, scenario="base", ledger=ledger)

    assert list(dev.columns) == DEVIATION_COLUMNS
    assert len(dev) == 3  # inner join on (code, year): MAT 2027 (no actual) and EXT (no plan) dropped
    dev = dev.set_index(["category_code", "year"])
    assert dev.loc[("REV", 2026), "plan"] == 20000.0
    assert dev.loc[("REV", 2026), "actual"] == 21000.0
    assert dev.loc[("REV", 2026), "deviation"] == 1000.0
    assert dev.loc[("REV", 2026), "deviation_pct"] == 0.05
    assert dev.loc[("MAT", 2026), "deviation"] == -50.0
    assert dev.loc[("MAT", 2026), "deviation_pct"] == -50.0 / 1200.0
    assert dev.loc[("PERS", 2026), "deviation"] == 0.0
    assert dev.loc[("PERS", 2026), "deviation_pct"] == 0.0
    assert dev.loc[("REV", 2026), "derivation_key"] == "dev:base:REV:2026"


def test_deviation_pct_none_when_plan_zero():
    ledger = DerivationLedger()
    plan = _plan_frame(ledger, [("OTH", 2026, 0.0)])
    actual = _actual_frame([("OTH", 2026, 42.0)])
    dev = compute_deviation(plan, actual, scenario="base", ledger=ledger)
    assert dev.loc[0, "deviation"] == 42.0
    assert dev.loc[0, "deviation_pct"] is None
    assert ledger.get("dev:base:OTH:2026").parameters["deviation_pct"] is None
    row = DeviationRow.build("OTH", 2026, 0.0, 42.0, "plan:base:OTH:2026")
    assert row.deviation_pct is None and row.deviation == 42.0


def test_deviation_derivations_have_resolvable_parents():
    ledger = DerivationLedger()
    plan = _plan_frame(ledger, [("REV", 2026, 100.0), ("MAT", 2026, 10.0)])
    actual = _actual_frame([("REV", 2026, 110.0), ("MAT", 2026, 9.0)])
    compute_deviation(plan, actual, scenario="base", ledger=ledger)

    assert ledger.missing_parents() == {}
    ledger.validate()
    d = ledger.get(dev_key("base", "MAT", 2026))
    assert d.parents == ("plan:base:MAT:2026", actual_key("MAT", 2026))
    assert d.inputs == {"plan": 10.0, "actual": 9.0}
    assert d.formula_text.startswith("Deviation_t = Actual_t - Plan_t")
    anchor = ledger.get("actual:MAT:2026")
    assert anchor.inputs["value"] == 9.0 and anchor.parents == ()
    json.dumps(d.inputs), json.dumps(d.parameters)
    # lineage: dev -> plan + actual anchor
    assert set(ledger.ancestors(d.key)) == {"plan:base:MAT:2026", "actual:MAT:2026"}


def test_existing_actual_anchor_is_reused_not_duplicated():
    ledger = DerivationLedger()
    ledger.add(actual_key("REV", 2025), "Actual Revenue_2025 (source data)", inputs={"value": 5.0})
    plan = _plan_frame(ledger, [("REV", 2025, 4.0)])
    compute_deviation(plan, _actual_frame([("REV", 2025, 5.0)]), scenario="base", ledger=ledger)
    assert ledger.get("actual:REV:2025").inputs["value"] == 5.0
    assert "dev:base:REV:2025" in ledger


def test_scenario_filter_and_years_filter():
    ledger = DerivationLedger()
    base = _plan_frame(ledger, [("REV", 2026, 100.0), ("REV", 2027, 110.0)], scenario="base")
    best = _plan_frame(ledger, [("REV", 2026, 108.0), ("REV", 2027, 118.8)], scenario="best")
    plan = pd.concat([base, best], ignore_index=True)
    actual = _actual_frame([("REV", 2026, 105.0), ("REV", 2027, 112.0)])

    dev_best = compute_deviation(plan, actual, scenario="best", ledger=ledger, years=[2026])
    assert len(dev_best) == 1
    assert dev_best.loc[0, "deviation"] == pytest.approx(-3.0)
    assert dev_best.loc[0, "derivation_key"] == "dev:best:REV:2026"
    dev_base = compute_deviation(plan, actual, scenario="base", ledger=ledger)
    assert dev_base["deviation"].tolist() == [5.0, 2.0]
    ledger.validate()
    # re-registering the same scenario/year raises (ledger refuses duplicate keys)
    with pytest.raises(KeyError):
        compute_deviation(plan, actual, scenario="base", ledger=ledger)


def test_compute_deviation_rejects_bad_frames():
    ledger = DerivationLedger()
    with pytest.raises(ValueError):
        compute_deviation(pd.DataFrame({"category_code": [], "year": [], "value": []}),
                          _actual_frame([]), scenario="base", ledger=ledger)
    plan = _plan_frame(ledger, [("REV", 2026, 1.0)])
    dup = pd.concat([plan, plan], ignore_index=True)
    with pytest.raises(ValueError):
        compute_deviation(dup, _actual_frame([("REV", 2026, 1.0)]), scenario="base", ledger=DerivationLedger())


# --------------------------------------------------------------------------- decomposition


def _fit(code="PERS", alpha=6000.0, beta=0.3, v=0.028, t0=2025, component=None) -> FitResult:
    return FitResult(category_code=code, alpha=alpha, beta=beta, r_squared=0.99, valorization_rate=v,
                     window_from=t0 - 4, window_to=t0, n=5, derivation_key=f"param:{code}",
                     component_code=component)


def test_decompose_pure_revenue_miss_has_zero_residual():
    fit = _fit()
    t, t0 = 2027, 2025
    fixed = fit.alpha * (1 + fit.valorization_rate) ** (t - t0)
    plan_rev, actual_rev = 22000.0, 23500.0
    plan_cost = fixed + fit.beta * plan_rev
    actual_cost = fixed + fit.beta * actual_rev  # alpha, v unchanged: only revenue moved
    ledger = DerivationLedger()
    ledger.add("param:PERS", "fit", parameters={"alpha": fit.alpha})
    out = decompose_cost_deviation(fit, plan_rev, actual_rev, plan_cost, actual_cost, t=t, t0=t0,
                                   scenario="base", ledger=ledger)
    assert out["deviation"] == pytest.approx(actual_cost - plan_cost)
    assert out["revenue_driven"] == pytest.approx(fit.beta * 1500.0)
    assert out["residual"] == pytest.approx(0.0, abs=1e-9)
    assert out["component_driven"] == 0.0
    assert out["revenue_driven_share"] == pytest.approx(1.0)
    assert out["planned_fixed_part"] == pytest.approx(fixed)
    assert out["implied_fixed_part"] == pytest.approx(fixed)
    assert out["derivation_key"] == devx_key("base", "PERS", 2027)
    d = ledger.get("devx:base:PERS:2027")
    assert d.parents == ("param:PERS",)
    assert d.parameters["residual"] == pytest.approx(0.0, abs=1e-9)
    ledger.validate()


def test_decompose_parts_sum_to_deviation_with_fixed_shift():
    fit = _fit(code="MAT", alpha=300.0, beta=0.045, v=0.02)
    t, t0 = 2026, 2025
    plan_rev, actual_rev = 22000.0, 21000.0
    plan_cost = fit.fixed_part_at(t) + fit.beta * plan_rev
    actual_cost = plan_cost + fit.beta * (actual_rev - plan_rev) + 12.5  # revenue miss + 12.5 of fixed overrun
    out = decompose_cost_deviation(fit, plan_rev, actual_rev, plan_cost, actual_cost, t=t, t0=t0)
    assert out["revenue_driven"] == pytest.approx(-45.0)
    assert out["residual"] == pytest.approx(12.5)
    assert out["revenue_driven"] + out["component_driven"] + out["residual"] == pytest.approx(out["deviation"])
    assert out["deviation_pct"] == pytest.approx(out["deviation"] / plan_cost)
    assert out["derivation_key"] is None  # no ledger given -> nothing registered


def test_decompose_with_component_for_oth():
    fit = _fit(code="OTH", alpha=500.0, beta=0.025, v=0.025, component="DEPR")
    t, t0 = 2026, 2025
    plan_rev, actual_rev = 22000.0, 22000.0
    plan_depr, actual_depr = 494.28, 520.0
    plan_cost = fit.fixed_part_at(t) + fit.beta * plan_rev + plan_depr
    actual_cost = fit.fixed_part_at(t) + fit.beta * actual_rev + actual_depr  # only depreciation moved
    ledger = DerivationLedger()
    ledger.add("param:OTH", "fit")
    ledger.add("plan:base:OTH:2026", "plan")
    ledger.add(actual_key("OTH", 2026), "actual", inputs={"value": actual_cost})
    out = decompose_cost_deviation(fit, plan_rev, actual_rev, plan_cost, actual_cost, t=t, t0=t0,
                                   scenario="base", ledger=ledger, plan_component=plan_depr,
                                   actual_component=actual_depr, plan_key="plan:base:OTH:2026")
    assert out["component_code"] == "DEPR"
    assert out["revenue_driven"] == 0.0
    assert out["component_driven"] == pytest.approx(actual_depr - plan_depr)
    assert out["residual"] == pytest.approx(0.0, abs=1e-9)
    d = ledger.get("devx:base:OTH:2026")
    assert d.parents == ("param:OTH", "plan:base:OTH:2026", "actual:OTH:2026")
    ledger.validate()
