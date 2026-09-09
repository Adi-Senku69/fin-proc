"""Investment plan -> straight-line depreciation schedule.

Mirrors ``nvplan.data.generate.generate_investment_plan`` exactly: each capex
tranche of year ``y`` is charged ``capex / life_years`` in years
``y, y+1, ..., y+life_years-1``; charges falling after the last year of the
plan are dropped (not accrued). Net book value = cumulative capex - cumulative
depreciation (``generate.net_book_value``).
"""

from __future__ import annotations

import pandas as pd

from nvplan.core.ledger import DerivationLedger

__all__ = ["depreciation_schedule", "depr_key", "capex_key", "DEFAULT_LIFE_YEARS"]

DEFAULT_LIFE_YEARS = 5


def depr_key(year: int) -> str:
    return f"depr:{int(year)}"


def capex_key(year: int) -> str:
    return f"capex:{int(year)}"


def depreciation_schedule(
    investment_plan: pd.DataFrame,
    life_years: int = DEFAULT_LIFE_YEARS,
    *,
    ledger: DerivationLedger,
) -> pd.DataFrame:
    """Return ``DataFrame(year, capex, depreciation, nbv)`` for every plan year.

    Registers ``capex:{year}`` (source anchor) and ``depr:{year}`` with inputs
    = the tranches contributing to that year's charge, parents = the capex keys.
    """
    if life_years <= 0:
        raise ValueError("life_years must be positive")
    for col in ("year", "capex"):
        if col not in investment_plan.columns:
            raise ValueError(f"investment plan needs column {col!r}")
    plan = investment_plan[["year", "capex"]].copy()
    plan["year"] = plan["year"].astype(int)
    plan["capex"] = plan["capex"].astype(float)
    if plan["year"].duplicated().any():
        raise ValueError("investment plan has duplicate years")
    plan = plan.sort_values("year").reset_index(drop=True)
    years = plan["year"].tolist()
    capex = dict(zip(years, plan["capex"].tolist()))
    year_set = set(years)

    for y in years:
        ledger.add(
            capex_key(y),
            f"Capex_{y} (investment plan, source data)",
            inputs={"year": y, "capex": capex[y]},
            parameters={},
        )

    charge = {y: 0.0 for y in years}
    tranches: dict[int, list[dict[str, float]]] = {y: [] for y in years}
    for y in years:
        per_year = capex[y] / life_years
        for k in range(life_years):
            target = y + k
            if target in year_set:  # charges beyond the plan horizon are dropped, as the generator does
                charge[target] += per_year
                tranches[target].append(
                    {"capex_year": y, "capex": capex[y], "charge": per_year, "age": k}
                )

    rows = []
    cum_capex = cum_depr = 0.0
    for y in years:
        cum_capex += capex[y]
        cum_depr += charge[y]
        nbv = cum_capex - cum_depr
        rows.append({"year": y, "capex": capex[y], "depreciation": charge[y], "nbv": nbv})
        ledger.add(
            depr_key(y),
            f"DEPR_t = sum over capex tranches y in [t-{life_years - 1}, t] of Capex_y / {life_years} "
            f"(straight-line over {life_years} years from the capex year)",
            inputs={"tranches": tranches[y]},
            parameters={"life_years": life_years, "t": y, "nbv": nbv},
            parents=tuple(capex_key(tr["capex_year"]) for tr in tranches[y]),
        )
    out = pd.DataFrame(rows, columns=["year", "capex", "depreciation", "nbv"])
    return out
