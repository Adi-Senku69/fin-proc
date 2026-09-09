"""Deviation plan versus actual: pure arithmetic, every figure registered.

    Deviation_c,t     = Actual_c,t - Plan_c,t
    DeviationPct_c,t  = Deviation_c,t / Plan_c,t          (None when Plan_c,t == 0)

Registered as ``dev:{scenario}:{code}:{year}`` with parents = the plan value's
derivation key and an ``actual:{code}:{year}`` anchor (same key scheme as
:func:`nvplan.core.regression.revenue_default_path` uses for its revenue anchor).

:func:`decompose_cost_deviation` splits a cost category's deviation into the
part explained by the revenue miss through the fitted beta and the residual
(the "contributing figures" the AI explanation must cite); registered as
``devx:{scenario}:{code}:{year}``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import pandas as pd

from nvplan.core.ledger import LONG_COLUMNS, DerivationLedger
from nvplan.core.regression import FitResult, actual_key

__all__ = [
    "DEVIATION_COLUMNS",
    "DeviationRow",
    "compute_deviation",
    "decompose_cost_deviation",
    "dev_key",
    "devx_key",
    "ensure_actual_anchor",
]

DEVIATION_COLUMNS = ["category_code", "year", "plan", "actual", "deviation", "deviation_pct", "derivation_key"]
DEVIATION_FORMULA = "Deviation_t = Actual_t - Plan_t; DeviationPct_t = Deviation_t / Plan_t"
DECOMPOSITION_FORMULA = (
    "Deviation_t = Actual_t - Plan_t = revenue_driven + component_driven + residual; "
    "revenue_driven = beta * (ActualRevenue_t - PlanRevenue_t); "
    "component_driven = ActualComponent_t - PlanComponent_t; "
    "residual = Deviation_t - revenue_driven - component_driven "
    "(= implied fixed part (Actual_t - beta*ActualRevenue_t - ActualComponent_t) "
    "- planned fixed part alpha*(1+v)^(t-t0))"
)


# --------------------------------------------------------------------------- keys


def dev_key(scenario: str, code: str, year: int) -> str:
    return f"dev:{scenario}:{code}:{int(year)}"


def devx_key(scenario: str, code: str, year: int) -> str:
    return f"devx:{scenario}:{code}:{int(year)}"


def ensure_actual_anchor(ledger: DerivationLedger, code: str, year: int, value: float) -> str:
    """Register ``actual:{code}:{year}`` (source-data anchor) unless already present."""
    key = actual_key(code, year)
    if key not in ledger:
        ledger.add(
            key,
            f"Actual {code}_{int(year)} (source data)",
            inputs={"year": int(year), "value": float(value), "category_code": str(code)},
            parameters={},
        )
    return key


# --------------------------------------------------------------------------- row


@dataclass(frozen=True)
class DeviationRow:
    category_code: str
    year: int
    plan: float
    actual: float
    deviation: float  # actual - plan
    deviation_pct: float | None  # deviation / plan, None when plan == 0
    plan_derivation_key: str

    @classmethod
    def build(cls, category_code: str, year: int, plan: float, actual: float, plan_derivation_key: str) -> DeviationRow:
        plan = float(plan)
        actual = float(actual)
        deviation = actual - plan
        pct = None if plan == 0.0 else deviation / plan
        return cls(
            category_code=str(category_code),
            year=int(year),
            plan=plan,
            actual=actual,
            deviation=deviation,
            deviation_pct=pct,
            plan_derivation_key=str(plan_derivation_key),
        )


# --------------------------------------------------------------------------- plan vs actual


def compute_deviation(
    plan_long: pd.DataFrame,
    actual_long: pd.DataFrame,
    *,
    scenario: str,
    ledger: DerivationLedger,
    years: Iterable[int] | None = None,
) -> pd.DataFrame:
    """Plan minus actual per (category_code, year), registered per cell.

    ``plan_long`` needs ``category_code, year, value, derivation_key`` (+ optional
    ``scenario``, filtered to ``scenario`` when present); ``actual_long`` needs
    ``category_code, year, value``. Only (code, year) pairs present in *both*
    frames are compared; ``years`` restricts further.

    Returns ``DataFrame(category_code, year, plan, actual, deviation,
    deviation_pct, derivation_key)`` with ``derivation_key = dev:{scenario}:{code}:{year}``.
    """
    need = set(LONG_COLUMNS) | {"derivation_key"}
    missing = need - set(plan_long.columns)
    if missing:
        raise ValueError(f"plan frame missing columns: {sorted(missing)}")
    missing = set(LONG_COLUMNS) - set(actual_long.columns)
    if missing:
        raise ValueError(f"actual frame missing columns: {sorted(missing)}")

    plan = plan_long
    if "scenario" in plan.columns:
        plan = plan[plan["scenario"].astype(str) == str(scenario)]
    plan = plan[list(need)].copy()
    plan["category_code"] = plan["category_code"].astype(str)
    plan["year"] = plan["year"].astype(int)
    plan["value"] = plan["value"].astype(float)
    plan["derivation_key"] = plan["derivation_key"].astype(str)
    if plan.duplicated(["category_code", "year"]).any():
        dup = plan[plan.duplicated(["category_code", "year"], keep=False)]
        raise ValueError(f"duplicate (category_code, year) pairs in plan frame for scenario {scenario!r}:\n{dup}")

    actual = actual_long[LONG_COLUMNS].copy()
    actual["category_code"] = actual["category_code"].astype(str)
    actual["year"] = actual["year"].astype(int)
    actual["value"] = actual["value"].astype(float)
    if actual.duplicated(["category_code", "year"]).any():
        dup = actual[actual.duplicated(["category_code", "year"], keep=False)]
        raise ValueError(f"duplicate (category_code, year) pairs in actual frame:\n{dup}")

    if years is not None:
        wanted = {int(y) for y in years}
        plan = plan[plan["year"].isin(wanted)]
        actual = actual[actual["year"].isin(wanted)]

    merged = plan.merge(
        actual, on=["category_code", "year"], how="inner", suffixes=("_plan", "_actual")
    ).sort_values(["category_code", "year"]).reset_index(drop=True)

    rows: list[dict[str, Any]] = []
    for r in merged.itertuples(index=False):
        row = DeviationRow.build(r.category_code, r.year, r.value_plan, r.value_actual, r.derivation_key)
        a_key = ensure_actual_anchor(ledger, row.category_code, row.year, row.actual)
        key = dev_key(scenario, row.category_code, row.year)
        ledger.add(
            key,
            DEVIATION_FORMULA,
            inputs={"plan": row.plan, "actual": row.actual},
            parameters={
                "scenario": str(scenario),
                "category_code": row.category_code,
                "t": row.year,
                "deviation": row.deviation,
                "deviation_pct": row.deviation_pct,
            },
            parents=(row.plan_derivation_key, a_key),
        )
        rows.append(
            {
                "category_code": row.category_code,
                "year": row.year,
                "plan": row.plan,
                "actual": row.actual,
                "deviation": row.deviation,
                "deviation_pct": row.deviation_pct,
                "derivation_key": key,
            }
        )
    out = pd.DataFrame(rows, columns=DEVIATION_COLUMNS)
    out["deviation_pct"] = out["deviation_pct"].astype(object)  # keep None (not NaN) for plan == 0
    return out


# --------------------------------------------------------------------------- decomposition


def decompose_cost_deviation(
    fit: FitResult,
    plan_revenue: float,
    actual_revenue: float,
    plan_cost: float,
    actual_cost: float,
    *,
    t: int,
    t0: int,
    scenario: str = "base",
    ledger: DerivationLedger | None = None,
    plan_component: float = 0.0,
    actual_component: float = 0.0,
    plan_key: str | None = None,
) -> dict[str, Any]:
    """Split ``actual_cost - plan_cost`` for one cost category into contributing figures.

    * ``revenue_driven``   = beta * (actual_revenue - plan_revenue)
    * ``component_driven`` = actual_component - plan_component (DEPR for OTH; 0 otherwise)
    * ``residual``         = deviation - revenue_driven - component_driven, i.e. the
      change of the fixed part against ``alpha * (1+v)^(t-t0)``.

    The three parts sum to the deviation exactly (up to floating point). When
    ``ledger`` is given the result is registered as ``devx:{scenario}:{code}:{year}``
    with parents ``param:{code}`` (+ ``plan_key`` and the actual anchor when resolvable).
    """
    plan_revenue = float(plan_revenue)
    actual_revenue = float(actual_revenue)
    plan_cost = float(plan_cost)
    actual_cost = float(actual_cost)
    plan_component = float(plan_component)
    actual_component = float(actual_component)
    t, t0 = int(t), int(t0)

    alpha, beta, v = fit.alpha, fit.beta, fit.valorization_rate
    deviation = actual_cost - plan_cost
    revenue_deviation = actual_revenue - plan_revenue
    revenue_driven = beta * revenue_deviation
    component_driven = actual_component - plan_component
    residual = deviation - revenue_driven - component_driven

    planned_fixed = alpha * (1.0 + v) ** (t - t0)
    implied_fixed = actual_cost - beta * actual_revenue - actual_component

    out: dict[str, Any] = {
        "category_code": fit.category_code,
        "scenario": str(scenario),
        "t": t,
        "t0": t0,
        "plan_revenue": plan_revenue,
        "actual_revenue": actual_revenue,
        "revenue_deviation": revenue_deviation,
        "plan_cost": plan_cost,
        "actual_cost": actual_cost,
        "deviation": deviation,
        "deviation_pct": None if plan_cost == 0.0 else deviation / plan_cost,
        "alpha": alpha,
        "beta": beta,
        "v": v,
        "planned_fixed_part": planned_fixed,
        "planned_variable_part": beta * plan_revenue,
        "implied_fixed_part": implied_fixed,
        "revenue_driven": revenue_driven,
        "component_driven": component_driven,
        "residual": residual,
        "revenue_driven_share": None if deviation == 0.0 else revenue_driven / deviation,
        "residual_share": None if deviation == 0.0 else residual / deviation,
        "derivation_key": None,
    }
    if fit.component_code is not None:
        out["component_code"] = fit.component_code
        out["plan_component"] = plan_component
        out["actual_component"] = actual_component

    if ledger is not None:
        key = devx_key(scenario, fit.category_code, t)
        parents: list[str] = [fit.derivation_key] if fit.derivation_key else []
        if plan_key is not None:
            parents.append(str(plan_key))
        a_key = actual_key(fit.category_code, t)
        if a_key in ledger:
            parents.append(a_key)
        ledger.add(
            key,
            DECOMPOSITION_FORMULA,
            inputs={
                "plan_revenue": plan_revenue,
                "actual_revenue": actual_revenue,
                "plan_cost": plan_cost,
                "actual_cost": actual_cost,
                "plan_component": plan_component,
                "actual_component": actual_component,
            },
            parameters={
                "alpha": alpha,
                "beta": beta,
                "v": v,
                "t": t,
                "t0": t0,
                "scenario": str(scenario),
                "deviation": deviation,
                "revenue_driven": revenue_driven,
                "component_driven": component_driven,
                "residual": residual,
                "planned_fixed_part": planned_fixed,
                "implied_fixed_part": implied_fixed,
            },
            parents=tuple(parents),
        )
        out["derivation_key"] = key
    return out
