"""P&L -> Balance Sheet (config mapping) -> Cash Flow (delta of BS movements).

Pure pandas functions, no DB. Every produced line carries a ``derivation_key``
and a :class:`LineDerivation` record (same field names as the ledger's
``Derivation`` so the orchestrator can merge the two dicts trivially).

Data contract
-------------
``pl_long``          long frame ``category_code, year, value`` for ONE scenario with
                     codes REV, MAT, EXT, PERS, OTH (total incl. depreciation) and
                     DEPR (component of OTH) for consecutive years.
``investment_plan``  frame ``year, capex`` (an optional ``depreciation`` column is
                     only used to derive an opening net book value).
``mapping``          dict loaded from ``bs_mapping.yaml`` (:func:`load_mapping`).

Design
------
* ``cash`` is NOT a balancing item. It is computed from the cash-flow statement
  (closing_cash = opening_cash + net_cash_flow) and written into the balance sheet.
  ``check_consistency`` then asserts the balance identity and the BS<->CF ties;
  nothing is ever force-balanced. (bs_mapping.yaml labels cash "balancing"; the
  two are algebraically identical when the formulas are right, and the explicit
  check is what makes that visible.)
* Depreciation used everywhere (P&L "depreciation" line, fixed-asset roll-forward,
  CF add-back) is the DEPR component of the P&L frame, so the three statements
  share one number.

Derivation keys: ``stmt:{scenario}:{pl|bs|cf}:{line_code}:{year}``.
P&L lines point to ``plan:{scenario}:{code}:{year}``; BS/CF lines point to the
P&L/BS/CF keys they use. The opening balance sheet (year = first_year - 1) is
included in the BS frame with ``mapping_ref = "opening_balance_sheet"`` so that
the first year's deltas and roll-forwards have a traceable parent.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from nvplan.config import DATA_DIR

__all__ = [
    "LineDerivation",
    "StatementSet",
    "load_mapping",
    "build_pl",
    "build_bs",
    "build_cf",
    "build_statements",
    "derive_opening",
    "check_consistency",
    "PL_LINES",
    "BS_LINES",
    "CF_LINES",
]

# --------------------------------------------------------------------------- records


@dataclass(frozen=True)
class LineDerivation:
    """Derivation record for one statement line (field names == ledger.Derivation)."""

    key: str
    formula_text: str
    inputs: dict[str, Any]
    parameters: dict[str, Any]
    parents: tuple[str, ...]


@dataclass
class StatementSet:
    pl: pd.DataFrame
    bs: pd.DataFrame
    cf: pd.DataFrame
    derivations: dict[str, LineDerivation] = field(default_factory=dict)
    scenario: str = ""


PL_LINES: list[str] = [
    "revenue", "material", "external", "personnel", "other", "depreciation",
    "total_costs", "ebit", "tax", "net_income",
]
BS_LINES: list[str] = [
    "cash", "receivables", "inventory", "fixed_assets", "total_assets",
    "payables", "equity", "total_liabilities_equity",
]
CF_LINES: list[str] = [
    "net_income", "depreciation", "delta_receivables", "delta_inventory", "delta_payables",
    "operating_cf", "investing_cf", "financing_cf", "net_cash_flow", "opening_cash", "closing_cash",
]

# P&L source lines: statement line code -> plan category code (mirrors mapping["pl"]["lines"]).
_PL_SOURCES: dict[str, str] = {
    "revenue": "REV", "material": "MAT", "external": "EXT", "personnel": "PERS",
    "other": "OTH", "depreciation": "DEPR",
}
# Brief API name -> code used in bs_mapping.yaml (kept as mapping_ref).
_CF_MAPPING_REF: dict[str, str] = {
    "operating_cf": "cf_operating", "investing_cf": "cf_investing",
    "financing_cf": "cf_financing", "net_cash_flow": "delta_cash",
}
_OPENING_KEYS = ("cash", "receivables", "inventory", "fixed_assets", "payables", "equity")

ABS_TOL = 1e-6


# --------------------------------------------------------------------------- helpers


def load_mapping(path: str | Path = DATA_DIR / "bs_mapping.yaml") -> dict:
    """Load the P&L -> BS -> CF mapping config (YAML) as a dict."""
    with open(path, encoding="utf-8") as fh:
        mapping = yaml.safe_load(fh)
    for section in ("assumptions", "bs", "cf"):
        if section not in mapping:
            raise ValueError(f"bs_mapping missing section '{section}': {path}")
    return mapping


def _key(scenario: str, stmt: str, line: str, year: int) -> str:
    return f"stmt:{scenario}:{stmt}:{line}:{int(year)}"


def _plan_key(scenario: str, code: str, year: int) -> str:
    return f"plan:{scenario}:{code}:{int(year)}"


def _record(ledger: dict[str, LineDerivation] | None, d: LineDerivation) -> str:
    if ledger is not None:
        ledger[d.key] = d
    return d.key


def _assumptions(mapping: dict) -> dict[str, float]:
    a = mapping["assumptions"]
    return {
        "dso_days": float(a["dso_days"]),
        "dpo_days": float(a["dpo_days"]),
        "inventory_days": float(a["inventory_days"]),
        "days_per_year": float(a.get("days_per_year", 365)),
    }


def _pl_wide(pl_df: pd.DataFrame) -> pd.DataFrame:
    """P&L statement frame -> wide (index year, columns line_code)."""
    return pl_df.pivot(index="year", columns="line_code", values="value").sort_index()


def _bs_wide(bs_df: pd.DataFrame) -> pd.DataFrame:
    return bs_df.pivot(index="year", columns="line_code", values="value").sort_index()


def _capex_by_year(investment_plan: pd.DataFrame, years: list[int]) -> dict[int, float]:
    inv = investment_plan.copy()
    inv["year"] = inv["year"].astype(int)
    if inv["year"].duplicated().any():
        raise ValueError("investment_plan has duplicate years")
    capex = inv.set_index("year")["capex"].astype(float)
    missing = [y for y in years if y not in capex.index]
    if missing:
        raise ValueError(f"investment_plan has no capex for years {missing}")
    return {int(y): float(capex.loc[y]) for y in years}


def _consecutive_years(pl_df: pd.DataFrame) -> list[int]:
    years = sorted(int(y) for y in pl_df["year"].unique())
    if not years:
        raise ValueError("empty P&L frame")
    if years != list(range(years[0], years[-1] + 1)):
        raise ValueError(f"P&L years are not consecutive: {years}")
    return years


# --------------------------------------------------------------------------- P&L


def build_pl(
    pl_long: pd.DataFrame,
    *,
    scenario: str,
    tax_rate: float,
    ledger: dict[str, LineDerivation] | None = None,
) -> pd.DataFrame:
    """Plan P&L long frame -> statement P&L ``(line_code, year, value, derivation_key)``.

    ``other`` (OTH) already INCLUDES depreciation, so total_costs = MAT+EXT+PERS+OTH
    and depreciation is shown as an "of which" line only.
    """
    required = {"category_code", "year", "value"}
    if not required.issubset(pl_long.columns):
        raise ValueError(f"pl_long needs columns {sorted(required)}")
    wide = (
        pl_long.assign(year=pl_long["year"].astype(int))
        .pivot(index="year", columns="category_code", values="value")
        .sort_index()
    )
    needed = set(_PL_SOURCES.values())
    missing = needed - set(wide.columns)
    if missing:
        raise ValueError(f"pl_long missing categories {sorted(missing)}")
    if wide[sorted(needed)].isna().any().any():
        raise ValueError("pl_long has missing values for some category/year")
    years = _consecutive_years(pl_long)

    rows: list[dict[str, Any]] = []

    def add(line: str, year: int, value: float, d: LineDerivation) -> None:
        _record(ledger, d)
        rows.append({"line_code": line, "year": int(year), "value": float(value), "derivation_key": d.key})

    for year in years:
        v: dict[str, float] = {}
        # source lines
        for line, code in _PL_SOURCES.items():
            v[line] = float(wide.loc[year, code])
            add(line, year, v[line], LineDerivation(
                key=_key(scenario, "pl", line, year),
                formula_text=f"{line} = {code}[{year}]",
                inputs={code: v[line]},
                parameters={},
                parents=(_plan_key(scenario, code, year),),
            ))
        # totals
        v["total_costs"] = v["material"] + v["external"] + v["personnel"] + v["other"]
        add("total_costs", year, v["total_costs"], LineDerivation(
            key=_key(scenario, "pl", "total_costs", year),
            formula_text="total_costs = material + external + personnel + other  (other incl. depreciation)",
            inputs={k: v[k] for k in ("material", "external", "personnel", "other")},
            parameters={},
            parents=tuple(_key(scenario, "pl", k, year) for k in ("material", "external", "personnel", "other")),
        ))
        v["ebit"] = v["revenue"] - v["total_costs"]
        add("ebit", year, v["ebit"], LineDerivation(
            key=_key(scenario, "pl", "ebit", year),
            formula_text="ebit = revenue - total_costs",
            inputs={"revenue": v["revenue"], "total_costs": v["total_costs"]},
            parameters={},
            parents=(_key(scenario, "pl", "revenue", year), _key(scenario, "pl", "total_costs", year)),
        ))
        v["tax"] = float(tax_rate) * max(v["ebit"], 0.0)
        add("tax", year, v["tax"], LineDerivation(
            key=_key(scenario, "pl", "tax", year),
            formula_text="tax = tax_rate * max(ebit, 0)",
            inputs={"ebit": v["ebit"]},
            parameters={"tax_rate": float(tax_rate)},
            parents=(_key(scenario, "pl", "ebit", year),),
        ))
        v["net_income"] = v["ebit"] - v["tax"]
        add("net_income", year, v["net_income"], LineDerivation(
            key=_key(scenario, "pl", "net_income", year),
            formula_text="net_income = ebit - tax",
            inputs={"ebit": v["ebit"], "tax": v["tax"]},
            parameters={},
            parents=(_key(scenario, "pl", "ebit", year), _key(scenario, "pl", "tax", year)),
        ))

    return pd.DataFrame(rows, columns=["line_code", "year", "value", "derivation_key"])


# --------------------------------------------------------------------------- opening BS


def derive_opening(
    pl_df: pd.DataFrame,
    investment_plan: pd.DataFrame,
    mapping: dict,
    *,
    cash: float,
) -> dict[str, float]:
    """Derive an opening balance sheet (end of first_year - 1) with the mapping formulas.

    No P&L exists for the opening year, so working-capital lines use the FIRST
    statement year's P&L as a steady-state proxy (receivables = revenue_t0 * DSO/365
    etc.). fixed_assets = net book value of investment-plan years before t0
    (capex - depreciation if a depreciation column exists, else 0). equity is set so
    the opening sheet balances at the given cash - exactly the convention used for
    the yaml's opening_balance_sheet ("equity set so that the sheet balances").
    """
    a = _assumptions(mapping)
    wide = _pl_wide(pl_df)
    t0 = int(wide.index.min())
    rev, mat, ext = (float(wide.loc[t0, k]) for k in ("revenue", "material", "external"))
    inv = investment_plan.copy()
    inv["year"] = inv["year"].astype(int)
    prior = inv[inv["year"] < t0]
    fa = float(prior["capex"].sum())
    if "depreciation" in prior.columns:
        fa -= float(prior["depreciation"].sum())
    receivables = rev * a["dso_days"] / a["days_per_year"]
    inventory = mat * a["inventory_days"] / a["days_per_year"]
    payables = (mat + ext) * a["dpo_days"] / a["days_per_year"]
    total_assets = float(cash) + receivables + inventory + fa
    return {
        "year": t0 - 1,
        "cash": float(cash),
        "receivables": receivables,
        "inventory": inventory,
        "fixed_assets": fa,
        "total_assets": total_assets,
        "payables": payables,
        "equity": total_assets - payables,
        "_derived": True,
        "_proxy_year": t0,
    }


def _opening_rows(
    opening: dict, scenario: str, year0: int, ledger: dict[str, LineDerivation] | None
) -> list[dict[str, Any]]:
    o = {k: float(opening[k]) for k in _OPENING_KEYS}
    derived = bool(opening.get("_derived", False))
    src = "derived opening (first-year P&L proxy)" if derived else "bs_mapping.yaml: opening_balance_sheet"
    rows = []

    def add(line: str, value: float, formula: str, inputs: dict, parents: tuple[str, ...] = ()) -> None:
        d = LineDerivation(
            key=_key(scenario, "bs", line, year0),
            formula_text=formula,
            inputs=inputs,
            parameters={"source": src, "opening_year": year0},
            parents=parents,
        )
        _record(ledger, d)
        rows.append({"line_code": line, "year": year0, "value": float(value),
                     "mapping_ref": "opening_balance_sheet", "derivation_key": d.key})

    for line in ("cash", "receivables", "inventory", "fixed_assets"):
        add(line, o[line], f"{line}[{year0}] = opening {line}", {line: o[line]})
    ta = o["cash"] + o["receivables"] + o["inventory"] + o["fixed_assets"]
    add("total_assets", ta, "total_assets = cash + receivables + inventory + fixed_assets",
        {k: o[k] for k in ("cash", "receivables", "inventory", "fixed_assets")},
        tuple(_key(scenario, "bs", k, year0) for k in ("cash", "receivables", "inventory", "fixed_assets")))
    for line in ("payables", "equity"):
        add(line, o[line], f"{line}[{year0}] = opening {line}", {line: o[line]})
    add("total_liabilities_equity", o["payables"] + o["equity"], "total_liabilities_equity = payables + equity",
        {"payables": o["payables"], "equity": o["equity"]},
        (_key(scenario, "bs", "payables", year0), _key(scenario, "bs", "equity", year0)))
    return rows


# --------------------------------------------------------------------------- BS + CF core


def _build_bs_cf(
    pl_df: pd.DataFrame,
    investment_plan: pd.DataFrame,
    mapping: dict,
    *,
    scenario: str,
    opening: dict | None,
    opening_cash: float,
    ledger: dict[str, LineDerivation] | None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Working-capital/fixed-asset/equity lines from the mapping, cash from the CF, then totals."""
    a = _assumptions(mapping)
    pl = _pl_wide(pl_df)
    years = [int(y) for y in pl.index]
    if years != list(range(years[0], years[-1] + 1)):
        raise ValueError(f"P&L years are not consecutive: {years}")
    year0 = years[0] - 1
    if opening is None:
        opening = derive_opening(pl_df, investment_plan, mapping, cash=opening_cash)
    elif "year" in opening and int(opening["year"]) != year0:
        raise ValueError(f"opening balance sheet is for {opening['year']}, statements start {years[0]}")
    missing = [k for k in _OPENING_KEYS if k not in opening]
    if missing:
        raise ValueError(f"opening balance sheet missing {missing}")
    capex = _capex_by_year(investment_plan, years)

    bs_rows: list[dict[str, Any]] = _opening_rows(opening, scenario, year0, ledger)
    cf_rows: list[dict[str, Any]] = []
    K = lambda stmt, line, y: _key(scenario, stmt, line, y)  # noqa: E731

    def add_bs(line: str, year: int, value: float, ref: str, formula: str, inputs: dict,
               parameters: dict, parents: tuple[str, ...]) -> None:
        d = LineDerivation(K("bs", line, year), formula, inputs, parameters, parents)
        _record(ledger, d)
        bs_rows.append({"line_code": line, "year": year, "value": float(value),
                        "mapping_ref": ref, "derivation_key": d.key})

    def add_cf(line: str, year: int, value: float, formula: str, inputs: dict,
               parameters: dict, parents: tuple[str, ...]) -> None:
        d = LineDerivation(K("cf", line, year), formula, inputs, parameters, parents)
        _record(ledger, d)
        cf_rows.append({"line_code": line, "year": year, "value": float(value),
                        "mapping_ref": _CF_MAPPING_REF.get(line, line), "derivation_key": d.key})

    prev = {k: float(opening[k]) for k in _OPENING_KEYS}
    for year in years:
        p = {k: float(pl.loc[year, k]) for k in ("revenue", "material", "external", "depreciation", "net_income")}
        cur: dict[str, float] = {}

        # ---- balance-sheet lines driven by the mapping formulas (all except cash)
        cur["receivables"] = p["revenue"] * a["dso_days"] / a["days_per_year"]
        add_bs("receivables", year, cur["receivables"], "bs.assets.receivables",
               "receivables = revenue * dso_days / days_per_year",
               {"revenue": p["revenue"]}, {"dso_days": a["dso_days"], "days_per_year": a["days_per_year"]},
               (K("pl", "revenue", year),))
        cur["inventory"] = p["material"] * a["inventory_days"] / a["days_per_year"]
        add_bs("inventory", year, cur["inventory"], "bs.assets.inventory",
               "inventory = material * inventory_days / days_per_year",
               {"material": p["material"]}, {"inventory_days": a["inventory_days"], "days_per_year": a["days_per_year"]},
               (K("pl", "material", year),))
        cur["fixed_assets"] = prev["fixed_assets"] + capex[year] - p["depreciation"]
        add_bs("fixed_assets", year, cur["fixed_assets"], "bs.assets.fixed_assets",
               "fixed_assets = prior_fixed_assets + capex - depreciation",
               {"prior_fixed_assets": prev["fixed_assets"], "capex": capex[year], "depreciation": p["depreciation"]},
               {"capex_source": "investment_plan"},
               (K("bs", "fixed_assets", year - 1), K("pl", "depreciation", year)))
        cur["payables"] = (p["material"] + p["external"]) * a["dpo_days"] / a["days_per_year"]
        add_bs("payables", year, cur["payables"], "bs.liabilities_equity.payables",
               "payables = (material + external) * dpo_days / days_per_year",
               {"material": p["material"], "external": p["external"]},
               {"dpo_days": a["dpo_days"], "days_per_year": a["days_per_year"]},
               (K("pl", "material", year), K("pl", "external", year)))
        cur["equity"] = prev["equity"] + p["net_income"]
        add_bs("equity", year, cur["equity"], "bs.liabilities_equity.equity",
               "equity = prior_equity + net_income",
               {"prior_equity": prev["equity"], "net_income": p["net_income"]}, {},
               (K("bs", "equity", year - 1), K("pl", "net_income", year)))

        # ---- cash-flow statement (delta of BS movements)
        add_cf("net_income", year, p["net_income"], "net_income = pl.net_income",
               {"net_income": p["net_income"]}, {}, (K("pl", "net_income", year),))
        add_cf("depreciation", year, p["depreciation"], "depreciation = pl.depreciation (non-cash add-back)",
               {"depreciation": p["depreciation"]}, {}, (K("pl", "depreciation", year),))
        deltas: dict[str, float] = {}
        for line in ("receivables", "inventory", "payables"):
            deltas[line] = cur[line] - prev[line]
            add_cf(f"delta_{line}", year, deltas[line], f"delta_{line} = {line}[t] - {line}[t-1]",
                   {f"{line}[t]": cur[line], f"{line}[t-1]": prev[line]}, {},
                   (K("bs", line, year), K("bs", line, year - 1)))
        op = p["net_income"] + p["depreciation"] - deltas["receivables"] - deltas["inventory"] + deltas["payables"]
        add_cf("operating_cf", year, op,
               "operating_cf = net_income + depreciation - delta_receivables - delta_inventory + delta_payables",
               {"net_income": p["net_income"], "depreciation": p["depreciation"], **{f"delta_{k}": v for k, v in deltas.items()}},
               {}, tuple(K("cf", k, year) for k in ("net_income", "depreciation", "delta_receivables", "delta_inventory", "delta_payables")))
        inv_cf = -capex[year]
        add_cf("investing_cf", year, inv_cf, "investing_cf = -capex", {"capex": capex[year]},
               {"capex_source": "investment_plan"}, ())
        add_cf("financing_cf", year, 0.0, "financing_cf = 0 (no financing in illustrative mapping)", {}, {}, ())
        ncf = op + inv_cf + 0.0
        add_cf("net_cash_flow", year, ncf, "net_cash_flow = operating_cf + investing_cf + financing_cf",
               {"operating_cf": op, "investing_cf": inv_cf, "financing_cf": 0.0}, {},
               tuple(K("cf", k, year) for k in ("operating_cf", "investing_cf", "financing_cf")))
        add_cf("opening_cash", year, prev["cash"], "opening_cash = cash[t-1]",
               {"cash[t-1]": prev["cash"]}, {}, (K("bs", "cash", year - 1),))
        cur["cash"] = prev["cash"] + ncf
        add_cf("closing_cash", year, cur["cash"], "closing_cash = opening_cash + net_cash_flow",
               {"opening_cash": prev["cash"], "net_cash_flow": ncf}, {},
               (K("cf", "opening_cash", year), K("cf", "net_cash_flow", year)))

        # ---- cash written into the BS from the CF, then totals
        add_bs("cash", year, cur["cash"], "cf.delta_cash",
               "cash = cf.closing_cash  (from cash-flow statement, not a balancing item)",
               {"closing_cash": cur["cash"]}, {}, (K("cf", "closing_cash", year),))
        ta = cur["cash"] + cur["receivables"] + cur["inventory"] + cur["fixed_assets"]
        add_bs("total_assets", year, ta, "bs.assets",
               "total_assets = cash + receivables + inventory + fixed_assets",
               {k: cur[k] for k in ("cash", "receivables", "inventory", "fixed_assets")}, {},
               tuple(K("bs", k, year) for k in ("cash", "receivables", "inventory", "fixed_assets")))
        tle = cur["payables"] + cur["equity"]
        add_bs("total_liabilities_equity", year, tle, "bs.liabilities_equity",
               "total_liabilities_equity = payables + equity",
               {"payables": cur["payables"], "equity": cur["equity"]}, {},
               (K("bs", "payables", year), K("bs", "equity", year)))
        prev = cur

    order = {line: i for i, line in enumerate(BS_LINES)}
    bs = pd.DataFrame(bs_rows, columns=["line_code", "year", "value", "mapping_ref", "derivation_key"])
    bs = bs.assign(_o=bs["line_code"].map(order)).sort_values(["year", "_o"]).drop(columns="_o").reset_index(drop=True)
    cf = pd.DataFrame(cf_rows, columns=["line_code", "year", "value", "mapping_ref", "derivation_key"])
    return bs, cf


def build_bs(
    pl_df: pd.DataFrame,
    investment_plan: pd.DataFrame,
    mapping: dict,
    *,
    scenario: str,
    opening: dict | None,
    opening_cash: float = 0.0,
    ledger: dict[str, LineDerivation] | None = None,
) -> pd.DataFrame:
    """Balance sheet ``(line_code, year, value, mapping_ref, derivation_key)``.

    Includes the opening year (first P&L year - 1). ``opening`` is the dict from
    ``mapping["opening_balance_sheet"]`` (or any dict with cash, receivables,
    inventory, fixed_assets, payables, equity); ``opening=None`` derives one via
    :func:`derive_opening` at ``opening_cash``. Cash comes from the cash-flow
    statement (built internally; :func:`build_cf` reproduces it).
    """
    bs, _ = _build_bs_cf(pl_df, investment_plan, mapping, scenario=scenario, opening=opening,
                         opening_cash=opening_cash, ledger=ledger)
    return bs


def build_cf(
    pl_df: pd.DataFrame,
    bs_df: pd.DataFrame,
    investment_plan: pd.DataFrame,
    *,
    scenario: str,
    ledger: dict[str, LineDerivation] | None = None,
) -> pd.DataFrame:
    """Cash-flow statement ``(line_code, year, value, mapping_ref, derivation_key)``.

    Built purely as deltas of ``bs_df`` (which must contain the opening year row):
    operating = NI + DEPR - dAR - dInv + dAP, investing = -capex, financing = 0,
    closing_cash = opening_cash + net_cash_flow with opening_cash = bs cash[t-1].
    """
    pl = _pl_wide(pl_df)
    bs = _bs_wide(bs_df)
    years = [int(y) for y in pl.index]
    capex = _capex_by_year(investment_plan, years)
    K = lambda stmt, line, y: _key(scenario, stmt, line, y)  # noqa: E731
    rows: list[dict[str, Any]] = []

    def add(line: str, year: int, value: float, formula: str, inputs: dict,
            parameters: dict, parents: tuple[str, ...]) -> None:
        d = LineDerivation(K("cf", line, year), formula, inputs, parameters, parents)
        _record(ledger, d)
        rows.append({"line_code": line, "year": year, "value": float(value),
                     "mapping_ref": _CF_MAPPING_REF.get(line, line), "derivation_key": d.key})

    for year in years:
        if year - 1 not in bs.index:
            raise ValueError(f"bs_df has no row for {year - 1} (needed for deltas of {year})")
        ni, dep = float(pl.loc[year, "net_income"]), float(pl.loc[year, "depreciation"])
        add("net_income", year, ni, "net_income = pl.net_income", {"net_income": ni}, {}, (K("pl", "net_income", year),))
        add("depreciation", year, dep, "depreciation = pl.depreciation (non-cash add-back)",
            {"depreciation": dep}, {}, (K("pl", "depreciation", year),))
        deltas: dict[str, float] = {}
        for line in ("receivables", "inventory", "payables"):
            c, pv = float(bs.loc[year, line]), float(bs.loc[year - 1, line])
            deltas[line] = c - pv
            add(f"delta_{line}", year, deltas[line], f"delta_{line} = {line}[t] - {line}[t-1]",
                {f"{line}[t]": c, f"{line}[t-1]": pv}, {}, (K("bs", line, year), K("bs", line, year - 1)))
        op = ni + dep - deltas["receivables"] - deltas["inventory"] + deltas["payables"]
        add("operating_cf", year, op,
            "operating_cf = net_income + depreciation - delta_receivables - delta_inventory + delta_payables",
            {"net_income": ni, "depreciation": dep, **{f"delta_{k}": v for k, v in deltas.items()}}, {},
            tuple(K("cf", k, year) for k in ("net_income", "depreciation", "delta_receivables", "delta_inventory", "delta_payables")))
        inv_cf = -capex[year]
        add("investing_cf", year, inv_cf, "investing_cf = -capex", {"capex": capex[year]}, {"capex_source": "investment_plan"}, ())
        add("financing_cf", year, 0.0, "financing_cf = 0 (no financing in illustrative mapping)", {}, {}, ())
        ncf = op + inv_cf + 0.0
        add("net_cash_flow", year, ncf, "net_cash_flow = operating_cf + investing_cf + financing_cf",
            {"operating_cf": op, "investing_cf": inv_cf, "financing_cf": 0.0}, {},
            tuple(K("cf", k, year) for k in ("operating_cf", "investing_cf", "financing_cf")))
        oc = float(bs.loc[year - 1, "cash"])
        add("opening_cash", year, oc, "opening_cash = cash[t-1]", {"cash[t-1]": oc}, {}, (K("bs", "cash", year - 1),))
        add("closing_cash", year, oc + ncf, "closing_cash = opening_cash + net_cash_flow",
            {"opening_cash": oc, "net_cash_flow": ncf}, {}, (K("cf", "opening_cash", year), K("cf", "net_cash_flow", year)))

    return pd.DataFrame(rows, columns=["line_code", "year", "value", "mapping_ref", "derivation_key"])


# --------------------------------------------------------------------------- orchestration


def build_statements(
    pl_long: pd.DataFrame,
    investment_plan: pd.DataFrame,
    mapping: dict,
    *,
    scenario: str,
    tax_rate: float,
    opening: dict | None,
    opening_cash: float = 0.0,
) -> StatementSet:
    """P&L -> BS -> CF for one scenario, with a derivation for every line."""
    ledger: dict[str, LineDerivation] = {}
    pl = build_pl(pl_long, scenario=scenario, tax_rate=tax_rate, ledger=ledger)
    bs, cf = _build_bs_cf(pl, investment_plan, mapping, scenario=scenario, opening=opening,
                          opening_cash=opening_cash, ledger=ledger)
    return StatementSet(pl=pl, bs=bs, cf=cf, derivations=ledger, scenario=scenario)


def check_consistency(stmts: StatementSet, *, tol: float = ABS_TOL) -> list[str]:
    """Genuine assertions (empty list = consistent):

    * BS balances every year: total_assets == total_liabilities_equity, and the totals
      equal the sum of their lines.
    * Cash tie: cash[t] - cash[t-1] == net_cash_flow[t] and cash[t] == closing_cash[t].
    * Fixed-asset roll-forward: fixed_assets[t] - fixed_assets[t-1] == capex - depreciation
      (capex read back from the CF's investing_cf).
    * Equity roll-forward: equity[t] - equity[t-1] == net_income[t].
    * P&L: ebit == revenue - total_costs, net_income == ebit - tax, tax >= 0.
    * Every derivation parent is a ``plan:`` key or exists in the set.
    """
    problems: list[str] = []
    bs, cf, pl = _bs_wide(stmts.bs), stmts.cf.pivot(index="year", columns="line_code", values="value"), _pl_wide(stmts.pl)

    def close(a: float, b: float) -> bool:
        return math.isfinite(a) and math.isfinite(b) and abs(a - b) <= tol

    for year in bs.index:
        r = bs.loc[year]
        ta = r["cash"] + r["receivables"] + r["inventory"] + r["fixed_assets"]
        tle = r["payables"] + r["equity"]
        if not close(r["total_assets"], ta):
            problems.append(f"{year}: total_assets {r['total_assets']} != sum of asset lines {ta}")
        if not close(r["total_liabilities_equity"], tle):
            problems.append(f"{year}: total_liabilities_equity {r['total_liabilities_equity']} != payables + equity {tle}")
        if not close(r["total_assets"], r["total_liabilities_equity"]):
            problems.append(
                f"{year}: balance sheet does not balance: assets {r['total_assets']} vs "
                f"liabilities+equity {r['total_liabilities_equity']} (diff {r['total_assets'] - r['total_liabilities_equity']})"
            )
    for year in cf.index:
        c = cf.loc[year]
        if year not in bs.index or (year - 1) not in bs.index:
            problems.append(f"{year}: cash-flow year without balance sheet rows for {year} and {year - 1}")
            continue
        d_cash = bs.loc[year, "cash"] - bs.loc[year - 1, "cash"]
        if not close(d_cash, c["net_cash_flow"]):
            problems.append(f"{year}: delta cash on BS {d_cash} != net_cash_flow {c['net_cash_flow']}")
        if not close(bs.loc[year, "cash"], c["closing_cash"]):
            problems.append(f"{year}: BS cash {bs.loc[year, 'cash']} != CF closing_cash {c['closing_cash']}")
        if not close(c["opening_cash"], bs.loc[year - 1, "cash"]):
            problems.append(f"{year}: CF opening_cash {c['opening_cash']} != BS cash[t-1] {bs.loc[year - 1, 'cash']}")
        d_fa = bs.loc[year, "fixed_assets"] - bs.loc[year - 1, "fixed_assets"]
        capex = -c["investing_cf"]
        if not close(d_fa, capex - c["depreciation"]):
            problems.append(f"{year}: delta fixed_assets {d_fa} != capex - depreciation {capex - c['depreciation']}")
        d_eq = bs.loc[year, "equity"] - bs.loc[year - 1, "equity"]
        if not close(d_eq, c["net_income"]):
            problems.append(f"{year}: delta equity {d_eq} != net_income {c['net_income']}")
        if year in pl.index:
            p = pl.loc[year]
            if not close(p["ebit"], p["revenue"] - p["total_costs"]):
                problems.append(f"{year}: ebit != revenue - total_costs")
            if not close(p["net_income"], p["ebit"] - p["tax"]):
                problems.append(f"{year}: net_income != ebit - tax")
            if p["tax"] < -tol:
                problems.append(f"{year}: negative tax {p['tax']}")
            if not close(c["net_income"], p["net_income"]):
                problems.append(f"{year}: CF net_income != P&L net_income")
    keys = set(stmts.derivations)
    for frame_name in ("pl", "bs", "cf"):
        frame = getattr(stmts, frame_name)
        for k in frame["derivation_key"]:
            if k not in keys:
                problems.append(f"{frame_name} line {k} has no derivation record")
    for d in stmts.derivations.values():
        for parent in d.parents:
            if not parent.startswith("plan:") and parent not in keys:
                problems.append(f"{d.key}: parent {parent} not in statement set")
    return problems
