"""Backtest: train on early years, predict a known year, MAPE / RMSE per category and horizon.

For every :class:`BacktestCase` (train window, target year, horizon):

1. ``fit_all`` on the train window (OTH net of DEPR, exactly as the live path);
2. two revenue paths for the target years:
   * **actual** revenue of the target year -> tests the cost cascade given known revenue
     (basis ``actual_revenue``; this is what the cost summary reports);
   * the **valorized default** revenue path from the train window -> tests the revenue
     default itself (basis ``default_revenue``; this is what the REV summary reports,
     and the cost rows along it are kept as an end-to-end secondary table);
3. ``project_scenario`` with ``t0 = train_window[1]``, compared with the actuals.

Errors are ``plan - actual``; ``abs_pct_error = |plan - actual| / |actual|``.
Verdict per (category, horizon) against the control-table thresholds
(``mape_threshold_pct: min 5, max 8``): ``within`` (MAPE <= 5%), ``marginal``
(5-8%), ``missed`` (> 8%). The report states the result honestly either way.

Fits and projections of each case are registered into a private scratch ledger
(their keys ``param:{code}`` / ``plan:...`` would collide across windows); only
the summary rows ``backtest:{code}:h{horizon}`` (and
``backtest:{code}:h{horizon}:default_revenue`` for the end-to-end cost rows) are
registered into the caller's ledger, with inputs = the list of (case, plan, actual).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from nvplan.core.ledger import DerivationLedger
from nvplan.core.projector import project_scenario
from nvplan.core.regression import (
    DEFAULT_COST_CODES,
    actual_key,
    default_revenue_key,
    fit_all,
    revenue_default_path,
)

__all__ = [
    "THRESHOLD_LOW",
    "THRESHOLD_HIGH",
    "ERROR_COLUMNS",
    "SUMMARY_COLUMNS",
    "BacktestCase",
    "BacktestResult",
    "backtest_key",
    "default_cases",
    "run_backtest",
    "summarize_errors",
    "verdict",
]

#: MAPE thresholds as fractions (control_table.yaml: backtest.mape_threshold_pct min 5 / max 8).
THRESHOLD_LOW = 0.05
THRESHOLD_HIGH = 0.08

BASIS_ACTUAL_REVENUE = "actual_revenue"
BASIS_DEFAULT_REVENUE = "default_revenue"

ERROR_COLUMNS = [
    "case", "train_from", "train_to", "target_year", "horizon", "category_code", "basis",
    "plan", "actual", "error", "abs_pct_error",
]
SUMMARY_COLUMNS = [
    "category_code", "horizon", "basis", "n", "mape", "rmse",
    "threshold_low", "threshold_high", "verdict", "derivation_key",
]


# --------------------------------------------------------------------------- keys


def backtest_key(code: str, horizon: int, basis: str = BASIS_ACTUAL_REVENUE) -> str:
    key = f"backtest:{code}:h{int(horizon)}"
    if basis != BASIS_ACTUAL_REVENUE:
        key += f":{basis}"
    return key


# --------------------------------------------------------------------------- cases


@dataclass(frozen=True)
class BacktestCase:
    train_window: tuple[int, int]
    target_year: int
    horizon: int

    def __post_init__(self) -> None:
        lo, hi = int(self.train_window[0]), int(self.train_window[1])
        object.__setattr__(self, "train_window", (lo, hi))
        object.__setattr__(self, "target_year", int(self.target_year))
        object.__setattr__(self, "horizon", int(self.horizon))
        if lo >= hi:
            raise ValueError(f"train_window must be (from < to), got {self.train_window}")
        if self.horizon < 1:
            raise ValueError(f"horizon must be >= 1, got {self.horizon}")
        if self.target_year != hi + self.horizon:
            raise ValueError(
                f"target_year {self.target_year} != train_window end {hi} + horizon {self.horizon}"
            )

    @property
    def label(self) -> str:
        return f"{self.train_window[0]}-{self.train_window[1]}->{self.target_year}"


def default_cases(
    years: Iterable[int],
    window_len: int = 5,
    horizons: Sequence[int] = (1, 2, 3),
) -> list[BacktestCase]:
    """Rolling windows: every ``window_len``-year train window and every horizon whose target year is in ``years``."""
    ys = sorted({int(y) for y in years})
    if window_len < 3:
        raise ValueError("window_len must be >= 3 (OLS with intercept)")
    if ys != list(range(ys[0], ys[-1] + 1)):
        raise ValueError(f"actual years must be contiguous, got {ys}")
    last = ys[-1]
    out: list[BacktestCase] = []
    for lo in ys:
        hi = lo + window_len - 1
        if hi >= last:
            break
        for h in horizons:
            target = hi + int(h)
            if target <= last:
                out.append(BacktestCase((lo, hi), target, int(h)))
    return out


# --------------------------------------------------------------------------- summary


def verdict(mape: float, low: float = THRESHOLD_LOW, high: float = THRESHOLD_HIGH) -> str:
    if not np.isfinite(mape):
        return "undefined"
    if mape <= low:
        return "within"
    if mape <= high:
        return "marginal"
    return "missed"


def summarize_errors(
    errors: pd.DataFrame,
    *,
    ledger: DerivationLedger | None = None,
    threshold_low: float = THRESHOLD_LOW,
    threshold_high: float = THRESHOLD_HIGH,
) -> pd.DataFrame:
    """MAPE / RMSE / n / verdict per (category_code, horizon, basis) of an error frame."""
    rows: list[dict[str, Any]] = []
    if errors.empty:
        return pd.DataFrame(rows, columns=SUMMARY_COLUMNS)
    for (code, h, basis), grp in errors.groupby(["category_code", "horizon", "basis"], sort=False):
        ape = grp["abs_pct_error"].to_numpy(dtype=float)
        err = grp["error"].to_numpy(dtype=float)
        n = int(len(grp))
        mape = float(np.nanmean(ape)) if np.isfinite(ape).any() else float("nan")
        rmse = float(np.sqrt(np.mean(err**2)))
        verd = verdict(mape, threshold_low, threshold_high)
        key = backtest_key(str(code), int(h), str(basis))
        if ledger is not None:
            ledger.add(
                key,
                "MAPE = mean_cases |Plan - Actual| / |Actual|; RMSE = sqrt(mean_cases (Plan - Actual)^2); "
                "verdict: within if MAPE <= threshold_low, marginal if <= threshold_high, else missed",
                inputs={
                    "cases": [
                        {
                            "case": str(r.case),
                            "train_from": int(r.train_from),
                            "train_to": int(r.train_to),
                            "target_year": int(r.target_year),
                            "plan": float(r.plan),
                            "actual": float(r.actual),
                            "abs_pct_error": float(r.abs_pct_error),
                        }
                        for r in grp.itertuples(index=False)
                    ],
                    "basis": str(basis),
                },
                parameters={
                    "category_code": str(code),
                    "horizon": int(h),
                    "n": n,
                    "mape": mape,
                    "rmse": rmse,
                    "threshold_low": float(threshold_low),
                    "threshold_high": float(threshold_high),
                    "verdict": verd,
                },
                parents=(),
                replace=True,
            )
        rows.append(
            {
                "category_code": str(code),
                "horizon": int(h),
                "basis": str(basis),
                "n": n,
                "mape": mape,
                "rmse": rmse,
                "threshold_low": float(threshold_low),
                "threshold_high": float(threshold_high),
                "verdict": verd,
                "derivation_key": key,
            }
        )
    out = pd.DataFrame(rows, columns=SUMMARY_COLUMNS)
    return out.sort_values(["basis", "category_code", "horizon"], kind="stable").reset_index(drop=True)


# --------------------------------------------------------------------------- result


def _md_table(df: pd.DataFrame, columns: list[tuple[str, str, str]]) -> str:
    """Tiny markdown renderer: columns = (frame column, header, format spec)."""
    header = "| " + " | ".join(h for _, h, _ in columns) + " |"
    sep = "|" + "|".join("---" for _ in columns) + "|"
    lines = [header, sep]
    for r in df.itertuples(index=False):
        cells = []
        for col, _, fmt in columns:
            val = getattr(r, col)
            if fmt and isinstance(val, (int, float, np.integer, np.floating)) and np.isfinite(val):
                cells.append(format(val, fmt))
            else:
                cells.append(str(val))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


@dataclass(frozen=True)
class BacktestResult:
    cases: tuple[BacktestCase, ...]
    errors: pd.DataFrame  # ERROR_COLUMNS, both bases
    summary: pd.DataFrame  # primary basis only: REV on default path, costs given actual revenue
    summary_default_path: pd.DataFrame  # secondary: costs along the default revenue path (end-to-end)
    threshold_low: float = THRESHOLD_LOW
    threshold_high: float = THRESHOLD_HIGH
    #: (alpha, beta, v, r2) of every train window, for the report
    fits: pd.DataFrame = field(default_factory=pd.DataFrame)

    @property
    def missed(self) -> pd.DataFrame:
        return self.summary[self.summary["verdict"] != "within"].reset_index(drop=True)

    def to_markdown(self) -> str:
        lo, hi = self.threshold_low * 100, self.threshold_high * 100
        windows = sorted({c.train_window for c in self.cases})
        lines = [
            "## Backtest (train on early years, predict a known year)",
            "",
            f"Cases: {len(self.cases)} — train windows "
            + ", ".join(f"{a}-{b}" for a, b in windows)
            + f"; horizons {sorted({c.horizon for c in self.cases})}.",
            f"Thresholds (control table): MAPE ≤ {lo:.0f}% within, {lo:.0f}–{hi:.0f}% marginal, > {hi:.0f}% missed.",
            "",
            "### Per category and horizon (REV = valorized default path; costs = cascade given the actual revenue of the target year)",
            "",
            _md_table(
                self.summary.assign(mape_pct=self.summary["mape"] * 100),
                [
                    ("category_code", "category", ""),
                    ("horizon", "h", "d"),
                    ("n", "n", "d"),
                    ("mape_pct", "MAPE %", ".2f"),
                    ("rmse", "RMSE (k€)", ".1f"),
                    ("verdict", "verdict", ""),
                ],
            ),
        ]
        n_missed = int((self.summary["verdict"] == "missed").sum())
        n_marg = int((self.summary["verdict"] == "marginal").sum())
        n_within = int((self.summary["verdict"] == "within").sum())
        lines += [
            "",
            f"Verdict count: {n_within} within, {n_marg} marginal, {n_missed} missed (of {len(self.summary)}).",
        ]
        if n_missed or n_marg:
            lines.append(
                "Rows not within threshold: "
                + ", ".join(f"{r.category_code} h{r.horizon} ({r.mape * 100:.2f}%, {r.verdict})" for r in self.missed.itertuples())
                + "."
            )
        if not self.summary_default_path.empty:
            lines += [
                "",
                "### End-to-end: costs along the valorized default revenue path (revenue error included)",
                "",
                _md_table(
                    self.summary_default_path.assign(mape_pct=self.summary_default_path["mape"] * 100),
                    [
                        ("category_code", "category", ""),
                        ("horizon", "h", "d"),
                        ("n", "n", "d"),
                        ("mape_pct", "MAPE %", ".2f"),
                        ("rmse", "RMSE (k€)", ".1f"),
                        ("verdict", "verdict", ""),
                    ],
                ),
            ]
        if not self.fits.empty:
            lines += [
                "",
                "### Parameters per train window",
                "",
                _md_table(
                    self.fits,
                    [
                        ("train_window", "window", ""),
                        ("category_code", "category", ""),
                        ("alpha", "alpha", ".2f"),
                        ("beta", "beta", ".4f"),
                        ("v", "v", ".4f"),
                        ("r_squared", "R²", ".4f"),
                    ],
                ),
            ]
        return "\n".join(lines)


# --------------------------------------------------------------------------- run


def _as_year_series(s: pd.Series, name: str) -> pd.Series:
    out = pd.Series(s).astype(float).copy()
    out.index = out.index.astype(int)
    out.name = name
    return out.sort_index()


def run_backtest(
    wide_actuals: pd.DataFrame,
    *,
    cases: Sequence[BacktestCase] | None = None,
    window_len: int = 5,
    horizons: Sequence[int] = (1, 2, 3),
    depreciation: pd.Series | None = None,
    ledger: DerivationLedger,
    cost_codes: Iterable[str] = DEFAULT_COST_CODES,
    revenue_code: str = "REV",
    component_code: str = "DEPR",
    threshold_low: float = THRESHOLD_LOW,
    threshold_high: float = THRESHOLD_HIGH,
) -> BacktestResult:
    """Run every case against ``wide_actuals`` (index year; columns REV, MAT, EXT, PERS, OTH, DEPR).

    ``depreciation`` defaults to the ``DEPR`` column. See the module docstring
    for the two revenue bases and what the summary reports.
    """
    wide = wide_actuals.copy()
    wide.index = wide.index.astype(int)
    wide = wide.sort_index()
    cost_codes = list(cost_codes)
    for c in [revenue_code, *cost_codes]:
        if c not in wide.columns:
            raise KeyError(f"wide actuals missing column {c!r}")
    if depreciation is None:
        if component_code not in wide.columns:
            raise ValueError(f"pass depreciation or include a {component_code!r} column")
        depreciation = wide[component_code]
    depr = _as_year_series(depreciation, component_code)

    case_list = list(default_cases(wide.index, window_len, horizons) if cases is None else cases)
    if not case_list:
        raise ValueError("no backtest cases")
    for c in case_list:
        if c.target_year not in wide.index:
            raise ValueError(f"target year {c.target_year} of case {c.label} not in actuals")
        if c.train_window[0] not in wide.index or c.train_window[1] not in wide.index:
            raise ValueError(f"train window of case {c.label} not covered by actuals")

    by_window: dict[tuple[int, int], list[BacktestCase]] = {}
    for c in case_list:
        by_window.setdefault(c.train_window, []).append(c)

    err_rows: list[dict[str, Any]] = []
    fit_rows: list[dict[str, Any]] = []
    for window, wcases in by_window.items():
        scratch = DerivationLedger()
        t0 = window[1]
        targets = sorted({c.target_year for c in wcases})
        horizon_of = {c.target_year: c.horizon for c in wcases}
        case_of = {c.target_year: c for c in wcases}

        fits = fit_all(wide, cost_codes, window, ledger=scratch, depreciation=depr, revenue_code=revenue_code,
                       component_code=component_code)
        for code, f in fits.items():
            fit_rows.append({
                "train_window": f"{window[0]}-{window[1]}", "category_code": code,
                "alpha": f.alpha, "beta": f.beta, "v": f.valorization_rate, "r_squared": f.r_squared,
            })

        # depreciation of a known year is source data: anchor it as actual:DEPR:{year}
        depr_keys: dict[int, str] = {}
        for y in targets:
            if y not in depr.index:
                raise ValueError(f"depreciation series missing target year {y}")
            k = actual_key(component_code, y)
            if k not in scratch:
                scratch.add(k, f"Actual {component_code}_{y} (source data)",
                            inputs={"year": y, "value": float(depr.loc[y]), "category_code": component_code})
            depr_keys[y] = k

        # (a) cost cascade given the actual revenue of the target year
        actual_rev = wide.loc[targets, revenue_code].astype(float)
        actual_rev.index = actual_rev.index.astype(int)
        act_keys: dict[int, str] = {}
        for y in targets:
            k = actual_key(revenue_code, y)
            if k not in scratch:
                scratch.add(k, f"Actual Revenue_{y} (source data)",
                            inputs={"year": y, "value": float(actual_rev.loc[y]), "category_code": revenue_code})
            act_keys[y] = k
        plan_a = project_scenario(
            fits, actual_rev, t0=t0, scenario=f"bt:{window[0]}-{window[1]}:{BASIS_ACTUAL_REVENUE}",
            depreciation=depr, ledger=scratch, revenue_path_keys=act_keys, revenue_path_label="actual",
            depreciation_keys=depr_keys, revenue_code=revenue_code, component_code=component_code,
        )
        # (b) valorized default revenue path from the train window, cascaded end-to-end
        default_rev = revenue_default_path(
            wide, window, (t0 + 1, targets[-1]), ledger=scratch, revenue_code=revenue_code
        ).loc[targets]
        plan_d = project_scenario(
            fits, default_rev, t0=t0, scenario=f"bt:{window[0]}-{window[1]}:{BASIS_DEFAULT_REVENUE}",
            depreciation=depr, ledger=scratch,
            revenue_path_keys={y: default_revenue_key(y) for y in targets},
            revenue_path_label="valorized", depreciation_keys=depr_keys,
            revenue_code=revenue_code, component_code=component_code,
        )
        scratch.validate()

        for basis, plan in ((BASIS_ACTUAL_REVENUE, plan_a), (BASIS_DEFAULT_REVENUE, plan_d)):
            codes = cost_codes if basis == BASIS_ACTUAL_REVENUE else [revenue_code, *cost_codes]
            for code in codes:
                sel = plan[plan["category_code"] == code].set_index("year")["value"]
                for y in targets:
                    p = float(sel.loc[y])
                    a = float(wide.loc[y, code])
                    e = p - a
                    ape = abs(e) / abs(a) if a != 0.0 else float("nan")
                    err_rows.append({
                        "case": case_of[y].label,
                        "train_from": window[0],
                        "train_to": window[1],
                        "target_year": y,
                        "horizon": horizon_of[y],
                        "category_code": code,
                        "basis": basis,
                        "plan": p,
                        "actual": a,
                        "error": e,
                        "abs_pct_error": ape,
                    })

    errors = pd.DataFrame(err_rows, columns=ERROR_COLUMNS)
    errors = errors.sort_values(["basis", "category_code", "horizon", "target_year"], kind="stable").reset_index(drop=True)

    primary = errors[
        ((errors["category_code"] == revenue_code) & (errors["basis"] == BASIS_DEFAULT_REVENUE))
        | ((errors["category_code"] != revenue_code) & (errors["basis"] == BASIS_ACTUAL_REVENUE))
    ]
    secondary = errors[(errors["category_code"] != revenue_code) & (errors["basis"] == BASIS_DEFAULT_REVENUE)]
    summary = summarize_errors(primary, ledger=ledger, threshold_low=threshold_low, threshold_high=threshold_high)
    order = {c: i for i, c in enumerate([revenue_code, *cost_codes])}
    summary = summary.assign(_o=summary["category_code"].map(order)).sort_values(["_o", "horizon"]).drop(columns="_o").reset_index(drop=True)
    summary_d = summarize_errors(secondary, ledger=ledger, threshold_low=threshold_low, threshold_high=threshold_high)
    summary_d = summary_d.assign(_o=summary_d["category_code"].map(order)).sort_values(["_o", "horizon"]).drop(columns="_o").reset_index(drop=True)

    return BacktestResult(
        cases=tuple(case_list),
        errors=errors,
        summary=summary,
        summary_default_path=summary_d,
        threshold_low=float(threshold_low),
        threshold_high=float(threshold_high),
        fits=pd.DataFrame(fit_rows, columns=["train_window", "category_code", "alpha", "beta", "v", "r_squared"]),
    )
