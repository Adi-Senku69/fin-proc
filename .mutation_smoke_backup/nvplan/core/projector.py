"""Five-year projection through the regression parameters, per scenario.

    PlanCost_c,t = alpha_c * (1 + v_c)^(t - t0) + beta_c * PlanRevenue_t
    PlanOTH_t    = alpha_OTH * (1 + v_OTH)^(t - t0) + beta_OTH * PlanRevenue_t + DEPR_t

``t0`` is the last actual year (``window_to``). Best / base / worst are three
revenue paths pushed through the same engine.

Output is a long frame: ``category_code, year, value, path, derivation_key``
(+ ``scenario`` from :func:`project_all`).

C1 -- structural refusal: a fit graded "review" (``nvplan.core.regression._grade_fit``) is not
projected. There is no fallback here the way there is for the valorization rate (0.0) or a
rejected joint fit (OLS) -- a "review" grade means the fixed/variable split itself does not
describe the category, and persisting a plan built on it would be presenting a number nobody
should trust as though it were one. :func:`project_scenario` raises ``ValueError`` instead, the
same exception type every other domain refusal in ``nvplan.core`` and ``nvplan.services`` already
raises (e.g. :func:`scenario_revenue_paths`'s bad-spread check, or ``services.planning.run_plan``'s
statement-inconsistency check) -- not a new exception type. ``nvplan.core.backtest.run_backtest``
-- which exists specifically to measure fits that may be weak -- filters "review"-graded
categories out before calling this rather than letting the refusal abort the whole report; see
its own docstring.
"""

from __future__ import annotations

from collections.abc import Mapping

import pandas as pd

from nvplan.core.depreciation import depr_key
from nvplan.core.ledger import DerivationLedger
from nvplan.core.regression import FitResult, default_revenue_key, param_key

__all__ = [
    "DEFAULT_SPREAD",
    "SCENARIOS",
    "PLAN_COLUMNS",
    "plan_key",
    "revenue_key",
    "project_scenario",
    "scenario_revenue_paths",
    "project_all",
]

DEFAULT_SPREAD: dict[str, float] = {"best": 0.08, "worst": -0.08}
SCENARIOS: tuple[str, ...] = ("base", "best", "worst")
PLAN_COLUMNS = ["category_code", "year", "value", "path", "derivation_key"]
COST_FORMULA = "alpha*(1+v)^(t-t0) + beta*PlanRevenue_t"


def plan_key(scenario: str, code: str, year: int) -> str:
    return f"plan:{scenario}:{code}:{int(year)}"


def revenue_key(scenario: str, year: int) -> str:
    return plan_key(scenario, "REV", year)


def _as_year_series(s: pd.Series, name: str) -> pd.Series:
    out = pd.Series(s).astype(float).copy()
    out.index = out.index.astype(int)
    out.name = name
    return out.sort_index()


# --------------------------------------------------------------------------- one scenario


def project_scenario(
    params: Mapping[str, FitResult],
    revenue_path: pd.Series,
    *,
    t0: int,
    scenario: str,
    depreciation: pd.Series,
    ledger: DerivationLedger,
    revenue_path_keys: Mapping[int, str],
    revenue_path_label: str = "valorized",
    depreciation_keys: Mapping[int, str] | None = None,
    revenue_code: str = "REV",
    component_code: str = "DEPR",
) -> pd.DataFrame:
    """Project every cost category along ``revenue_path`` (index = plan years).

    Emits REV rows (path ``revenue_path_label``, derivation from
    ``revenue_path_keys``), one cost row per fitted category (path
    ``cascaded``, derivation ``plan:{scenario}:{code}:{year}``) and DEPR rows
    (path ``cascaded``, derivation ``depr:{year}``) whenever a fit was made net
    of the component (OTH).

    Raises:
        ValueError: If ``revenue_path_keys`` is missing a year, or if any ``params`` entry
            is graded "review" (C1) -- refuse to project a fixed/variable split that has
            already been flagged as not describing its category. Callers that only want to
            *measure* fits that may be weak (``nvplan.core.backtest.run_backtest``) must
            filter those out of ``params`` before calling this, not catch the refusal here.
    """
    rev = _as_year_series(revenue_path, revenue_code)
    depr = _as_year_series(depreciation, component_code)
    years = [int(y) for y in rev.index]
    t0 = int(t0)
    missing_keys = [y for y in years if y not in revenue_path_keys]
    if missing_keys:
        raise ValueError(f"revenue_path_keys missing years {missing_keys}")
    review = {code: fit for code, fit in params.items() if fit.quality == "review"}
    if review:
        detail = "; ".join(f"{code} ({fit.quality_reason})" for code, fit in sorted(review.items()))
        msg = (
            f"Refusing to project {sorted(review)}: graded for review, not good/fair ({detail}). "
            "A category graded for review has no fallback that would be honest -- resolve the "
            "fit (a longer window, or a different method) before it can enter a plan."
        )
        raise ValueError(msg)

    rows: list[dict] = []
    for y in years:
        rows.append(
            {
                "category_code": revenue_code,
                "year": y,
                "value": float(rev.loc[y]),
                "path": revenue_path_label,
                "derivation_key": str(revenue_path_keys[y]),
            }
        )

    needs_component = [c for c, f in params.items() if f.component_code is not None]
    if needs_component:
        missing = [y for y in years if y not in depr.index]
        if missing:
            raise ValueError(f"depreciation series missing plan years {missing}")

    for code, fit in params.items():
        alpha, beta, v = fit.alpha, fit.beta, fit.valorization_rate
        for y in years:
            plan_rev = float(rev.loc[y])
            fixed = alpha * (1.0 + v) ** (y - t0)
            value = fixed + beta * plan_rev
            parents = [param_key(code), str(revenue_path_keys[y])]
            inputs = {"PlanRevenue_t": plan_rev}
            parameters = {"alpha": alpha, "beta": beta, "v": v, "t0": t0, "t": y,
                          "fixed_part_t": fixed, "variable_part_t": beta * plan_rev}
            formula = COST_FORMULA
            if fit.component_code is not None:
                d = float(depr.loc[y])
                value += d
                dk = depr_key(y) if depreciation_keys is None else str(depreciation_keys[y])
                parents.append(dk)
                inputs[f"{fit.component_code}_t"] = d
                formula = f"{COST_FORMULA} + {fit.component_code}_t"
            ledger.add(
                plan_key(scenario, code, y),
                formula,
                inputs=inputs,
                parameters=parameters,
                parents=tuple(parents),
            )
            rows.append(
                {
                    "category_code": code,
                    "year": y,
                    "value": float(value),
                    "path": "cascaded",
                    "derivation_key": plan_key(scenario, code, y),
                }
            )

    if needs_component:
        for y in years:
            dk = depr_key(y) if depreciation_keys is None else str(depreciation_keys[y])
            rows.append(
                {
                    "category_code": component_code,
                    "year": y,
                    "value": float(depr.loc[y]),
                    "path": "cascaded",
                    "derivation_key": dk,
                }
            )

    return pd.DataFrame(rows, columns=PLAN_COLUMNS)


# --------------------------------------------------------------------------- scenario revenue paths


def scenario_revenue_paths(
    base: pd.Series,
    spread: Mapping[str, float] | None = None,
    *,
    ledger: DerivationLedger,
    base_keys: Mapping[int, str] | None = None,
    revenue_code: str = "REV",
) -> dict[str, pd.Series]:
    """Best / worst revenue = base x (1 + spread). Returns ``{"base", "best", "worst"}``.

    Registers ``plan:{scenario}:REV:{year}`` for every scenario including
    ``base`` (a pass-through whose parent is the base path's own key). If
    ``base_keys`` is omitted the default-path keys ``plan:default:REV:{year}``
    are used when present in the ledger; otherwise an ``input:REV:{year}``
    anchor is registered for the externally supplied base value.
    """
    spread = dict(DEFAULT_SPREAD if spread is None else spread)
    if "base" in spread:
        raise ValueError("spread must not contain 'base'")
    base = _as_year_series(base, revenue_code)
    years = [int(y) for y in base.index]

    resolved: dict[int, str] = {}
    for y in years:
        if base_keys is not None:
            resolved[y] = str(base_keys[y])
        elif default_revenue_key(y) in ledger:
            resolved[y] = default_revenue_key(y)
        else:
            k = f"input:{revenue_code}:{y}"
            if k not in ledger:
                ledger.add(k, "Base revenue path supplied as input", inputs={"year": y, "value": float(base.loc[y])})
            resolved[y] = k

    out: dict[str, pd.Series] = {}
    for y in years:
        ledger.add(
            revenue_key("base", y),
            "PlanRevenue_base_t = PlanRevenue_t (base path)",
            inputs={"PlanRevenue_t": float(base.loc[y])},
            parameters={"t": y},
            parents=(resolved[y],),
        )
    out["base"] = base.copy()

    for scenario, s in spread.items():
        vals = {}
        for y in years:
            val = float(base.loc[y]) * (1.0 + float(s))
            ledger.add(
                revenue_key(scenario, y),
                "PlanRevenue_scenario_t = PlanRevenue_base_t * (1 + spread)",
                inputs={"PlanRevenue_base_t": float(base.loc[y])},
                parameters={"spread": float(s), "scenario": scenario, "t": y},
                parents=(revenue_key("base", y),),
            )
            vals[y] = val
        ser = pd.Series(vals, name=revenue_code, dtype=float)
        ser.index.name = "year"
        out[scenario] = ser
    return out


# --------------------------------------------------------------------------- all scenarios


def project_all(
    params: Mapping[str, FitResult],
    base_revenue: pd.Series,
    spread: Mapping[str, float] | None = None,
    *,
    t0: int,
    depreciation: pd.Series,
    ledger: DerivationLedger,
    base_keys: Mapping[int, str] | None = None,
    revenue_path_label: str = "valorized",
) -> pd.DataFrame:
    """Project base / best / worst. Returns the long frame with a ``scenario`` column."""
    paths = scenario_revenue_paths(base_revenue, spread, ledger=ledger, base_keys=base_keys)
    frames = []
    for scenario, path in paths.items():
        keys = {int(y): revenue_key(scenario, int(y)) for y in path.index}
        df = project_scenario(
            params, path, t0=t0, scenario=scenario, depreciation=depreciation, ledger=ledger,
            revenue_path_keys=keys, revenue_path_label=revenue_path_label,
        )
        df.insert(0, "scenario", scenario)
        frames.append(df)
    return pd.concat(frames, ignore_index=True)
