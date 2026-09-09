"""OLS split of each cost category into fixed (alpha) and variable (beta) parts.

Method (exactly as the PDF):

    Cost_t = alpha + beta * Revenue_t + eps_t         OLS over the window
    R^2    = 1 - SS_res / SS_tot
    f_t    = Cost_t - beta * Revenue_t                fixed part (= alpha + eps_t)
    v      = mean_t ( f_t / f_{t-1} - 1 )             valorization rate (avg YoY growth of f)

OTH is regressed *net of depreciation* (OTH - DEPR); :func:`fit_all` subtracts
the component series and records that in the derivation inputs.

Everything is pure: pandas / numpy in, numbers + ledger entries out.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from nvplan.core.ledger import CALC_VERSION, DerivationLedger, calc_version

__all__ = [
    "CALC_VERSION",
    "calc_version",
    "FitResult",
    "fit_category",
    "fit_all",
    "ols",
    "revenue_default_path",
    "revenue_growth_rate",
    "actual_key",
    "param_key",
    "default_revenue_key",
    "DEFAULT_COST_CODES",
]

DEFAULT_COST_CODES: tuple[str, ...] = ("MAT", "EXT", "PERS", "OTH")
DEFAULT_WINDOW: tuple[int, int] = (2021, 2025)
COMPONENT_CODE = "DEPR"  # the component subtracted from OTH before regression


# --------------------------------------------------------------------------- keys


def param_key(code: str) -> str:
    return f"param:{code}"


def actual_key(code: str, year: int) -> str:
    return f"actual:{code}:{int(year)}"


def default_revenue_key(year: int) -> str:
    return f"plan:default:REV:{int(year)}"


# --------------------------------------------------------------------------- result


@dataclass(frozen=True)
class FitResult:
    category_code: str
    alpha: float
    beta: float
    r_squared: float
    valorization_rate: float
    window_from: int
    window_to: int
    n: int
    points: list[dict[str, float]] = field(default_factory=list)
    fixed_part_series: dict[int, float] = field(default_factory=dict)
    derivation_key: str = ""
    #: component series subtracted from the raw cost before the fit (e.g. "DEPR" for OTH)
    component_code: str | None = None

    def fixed_part_at(self, year: int, t0: int | None = None) -> float:
        """alpha valorized to ``year``: alpha * (1+v)^(year - t0), t0 defaults to window_to."""
        t0 = self.window_to if t0 is None else t0
        return self.alpha * (1.0 + self.valorization_rate) ** (int(year) - int(t0))

    def as_parameter_row(self) -> dict[str, Any]:
        """Shape of the ``parameter`` table row (minus ids / timestamps)."""
        return {
            "category_code": self.category_code,
            "alpha": self.alpha,
            "beta": self.beta,
            "r_squared": self.r_squared,
            "valorization_rate": self.valorization_rate,
            "window_from": self.window_from,
            "window_to": self.window_to,
            "calc_version": CALC_VERSION,
            "derivation_key": self.derivation_key,
        }


# --------------------------------------------------------------------------- OLS


def ols(x: np.ndarray, y: np.ndarray) -> tuple[float, float, float, np.ndarray, np.ndarray]:
    """OLS of y on x with intercept via ``numpy.linalg.lstsq``.

    Returns ``(alpha, beta, r_squared, fitted, residuals)``.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    if x.shape != y.shape or x.ndim != 1:
        raise ValueError("x and y must be 1-d arrays of equal length")
    if len(x) < 3:
        raise ValueError(f"need at least 3 points for OLS with intercept, got {len(x)}")
    design = np.column_stack([np.ones_like(x), x])
    coef, *_ = np.linalg.lstsq(design, y, rcond=None)
    alpha, beta = float(coef[0]), float(coef[1])
    fitted = alpha + beta * x
    resid = y - fitted
    ss_res = float(np.sum(resid**2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    if ss_tot == 0.0:
        r2 = 1.0 if ss_res == 0.0 else 0.0
    else:
        r2 = 1.0 - ss_res / ss_tot
    return alpha, beta, r2, fitted, resid


def _window_years(wide: pd.DataFrame, window: tuple[int, int]) -> list[int]:
    lo, hi = int(window[0]), int(window[1])
    if lo >= hi:
        raise ValueError(f"window must be (from < to), got {window}")
    years = [int(y) for y in wide.index if lo <= int(y) <= hi]
    expected = list(range(lo, hi + 1))
    if years != expected:
        raise ValueError(f"window {window} not fully covered by actual years {list(wide.index)}")
    return years


def _mean_yoy_growth(values: Sequence[float]) -> tuple[float, list[float]]:
    vals = [float(v) for v in values]
    growth = [vals[i] / vals[i - 1] - 1.0 for i in range(1, len(vals))]
    return float(np.mean(growth)), growth


# --------------------------------------------------------------------------- per-category fit


def fit_category(
    wide: pd.DataFrame,
    code: str,
    window: tuple[int, int] = DEFAULT_WINDOW,
    *,
    ledger: DerivationLedger,
    revenue_code: str = "REV",
    subtract: pd.Series | None = None,
    subtract_code: str | None = None,
) -> FitResult:
    """Fit ``Cost_t = alpha + beta * Revenue_t`` for one category over ``window``.

    ``subtract`` (indexed by year) is removed from the raw cost before the fit
    (used for OTH - DEPR); ``subtract_code`` labels it in the derivation.
    Registers derivation ``param:{code}``.
    """
    if code not in wide.columns:
        raise KeyError(f"category {code!r} not in wide frame columns {list(wide.columns)}")
    if revenue_code not in wide.columns:
        raise KeyError(f"revenue code {revenue_code!r} not in wide frame")
    years = _window_years(wide, window)

    rev = wide.loc[years, revenue_code].to_numpy(dtype=float)
    cost_raw = wide.loc[years, code].to_numpy(dtype=float)
    if subtract is not None:
        sub = pd.Series(subtract).astype(float)
        sub.index = sub.index.astype(int)
        missing = [y for y in years if y not in sub.index]
        if missing:
            raise ValueError(f"subtract series missing years {missing}")
        sub_vals = sub.loc[years].to_numpy(dtype=float)
        subtract_code = subtract_code or "component"
    else:
        sub_vals = np.zeros_like(cost_raw)
    cost = cost_raw - sub_vals
    if np.isnan(rev).any() or np.isnan(cost).any():
        raise ValueError(f"NaN in window {window} for {code}")

    alpha, beta, r2, fitted, resid = ols(rev, cost)
    fixed = cost - beta * rev  # == alpha + resid
    v, growth = _mean_yoy_growth(fixed)

    points = [
        {
            "year": int(y),
            "revenue": float(r),
            "cost": float(c),
            "fitted": float(f),
            "residual": float(e),
        }
        for y, r, c, f, e in zip(years, rev, cost, fitted, resid)
    ]
    fixed_series = {int(y): float(f) for y, f in zip(years, fixed)}

    key = param_key(code)
    formula = (
        f"Cost_t = alpha + beta * Revenue_t + eps (OLS, window {years[0]}-{years[-1]}); "
        "v = mean YoY growth of (Cost_t - beta*Revenue_t)"
    )
    inputs: dict[str, Any] = {
        "points": points,
        "revenue_code": revenue_code,
        "cost_code": code,
        "fixed_part": [{"year": int(y), "value": float(f)} for y, f in zip(years, fixed)],
        "fixed_part_yoy_growth": [
            {"year": int(y), "growth": float(g)} for y, g in zip(years[1:], growth)
        ],
    }
    if subtract is not None:
        inputs["subtracted_code"] = subtract_code
        inputs["subtracted"] = [
            {"year": int(y), "cost_total": float(t), "component": float(s), "cost": float(c)}
            for y, t, s, c in zip(years, cost_raw, sub_vals, cost)
        ]
        formula = f"[{code} regressed net of {subtract_code}: Cost_t = {code}_t - {subtract_code}_t] " + formula
    ledger.add(
        key,
        formula,
        inputs=inputs,
        parameters={
            "alpha": alpha,
            "beta": beta,
            "r_squared": r2,
            "v": v,
            "n": len(years),
            "window_from": years[0],
            "window_to": years[-1],
            "calc_version": CALC_VERSION,
        },
        parents=(),
    )
    return FitResult(
        category_code=code,
        alpha=alpha,
        beta=beta,
        r_squared=r2,
        valorization_rate=v,
        window_from=years[0],
        window_to=years[-1],
        n=len(years),
        points=points,
        fixed_part_series=fixed_series,
        derivation_key=key,
        component_code=subtract_code if subtract is not None else None,
    )


def fit_all(
    wide: pd.DataFrame,
    codes: Iterable[str] = DEFAULT_COST_CODES,
    window: tuple[int, int] = DEFAULT_WINDOW,
    *,
    ledger: DerivationLedger,
    depreciation: pd.Series | None = None,
    revenue_code: str = "REV",
    component_of: str = "OTH",
    component_code: str = COMPONENT_CODE,
) -> dict[str, FitResult]:
    """Fit every cost category. ``component_of`` (OTH) is regressed net of ``depreciation``.

    If ``depreciation`` is None the ``DEPR`` column of ``wide`` is used when present.
    """
    codes = list(codes)
    if depreciation is None and component_code in wide.columns:
        depreciation = wide[component_code]
    out: dict[str, FitResult] = {}
    for code in codes:
        if code == component_of:
            if depreciation is None:
                raise ValueError(f"{component_of} must be regressed net of {component_code}: pass depreciation")
            out[code] = fit_category(
                wide, code, window, ledger=ledger, revenue_code=revenue_code,
                subtract=depreciation, subtract_code=component_code,
            )
        else:
            out[code] = fit_category(wide, code, window, ledger=ledger, revenue_code=revenue_code)
    return out


# --------------------------------------------------------------------------- revenue default path


def revenue_growth_rate(
    wide: pd.DataFrame,
    window: tuple[int, int] = DEFAULT_WINDOW,
    *,
    ledger: DerivationLedger,
    revenue_code: str = "REV",
) -> float:
    """Mean YoY revenue growth over the window; registers ``param:REV``."""
    years = _window_years(wide, window)
    rev = wide.loc[years, revenue_code].to_numpy(dtype=float)
    g, growth = _mean_yoy_growth(rev)
    ledger.add(
        param_key(revenue_code),
        f"g = mean YoY growth of Revenue_t over window {years[0]}-{years[-1]}: mean_t(Revenue_t / Revenue_(t-1) - 1)",
        inputs={
            "points": [{"year": int(y), "revenue": float(r)} for y, r in zip(years, rev)],
            "yoy_growth": [{"year": int(y), "growth": float(x)} for y, x in zip(years[1:], growth)],
        },
        parameters={"g": g, "n": len(years), "window_from": years[0], "window_to": years[-1],
                    "calc_version": CALC_VERSION},
    )
    return g


def revenue_default_path(
    wide: pd.DataFrame,
    window: tuple[int, int] = DEFAULT_WINDOW,
    plan_years: tuple[int, int] = (2026, 2030),
    *,
    ledger: DerivationLedger,
    revenue_code: str = "REV",
) -> pd.Series:
    """Valorized default revenue: last actual grown at the mean YoY growth over the window.

    Returns a Series indexed by plan year (name ``REV``). Registers
    ``actual:REV:{t0}`` (anchor), ``param:REV`` (growth) and
    ``plan:default:REV:{year}`` per plan year, each with parent = previous year's key.
    """
    years = _window_years(wide, window)
    t0 = years[-1]
    lo, hi = int(plan_years[0]), int(plan_years[1])
    if lo <= t0:
        raise ValueError(f"plan years {plan_years} must start after last actual year {t0}")
    g = revenue_growth_rate(wide, window, ledger=ledger, revenue_code=revenue_code)

    last = float(wide.loc[t0, revenue_code])
    anchor = actual_key(revenue_code, t0)
    ledger.add(
        anchor,
        f"Actual Revenue_{t0} (source data)",
        inputs={"year": t0, "value": last, "category_code": revenue_code},
        parameters={},
    )

    values: dict[int, float] = {}
    prev_key, prev_val = anchor, last
    for year in range(lo, hi + 1):
        val = prev_val * (1.0 + g)
        key = default_revenue_key(year)
        ledger.add(
            key,
            "PlanRevenue_t = PlanRevenue_(t-1) * (1 + g)",
            inputs={"PlanRevenue_prev": prev_val, "prev_year": year - 1},
            parameters={"g": g, "t": year, "t0": t0},
            parents=(prev_key, param_key(revenue_code)),
        )
        values[year] = val
        prev_key, prev_val = key, val
    s = pd.Series(values, name=revenue_code, dtype=float)
    s.index.name = "year"
    return s
