"""Deterministic ILLUSTRATIVE dummy-data generator (PLAN.md section 3).

Everything here is invented. Every output carries the label ``ILLUSTRATIVE``.
The generator is seeded (``SEED = 42``) so the regression engine can later be
checked against the parameters that produced the data (golden test).

Generating model (units k€, years 2016-2025):

    Revenue_t   = Revenue_{t-1} * (1 + g_t) * (1 + e_t)         g_t ~ U(5%, 7%), e_t ~ N(0, 0.5%)
    Cost_c,t    = alpha_c * (1 + v_c)^(t - 2016) + beta_c * Revenue_t + N(0, sigma_c * mean_level_c)

    mean_level_c = mean over years of the noiseless cost series.

"Other costs" (OTH) in ``actuals.csv`` is the TOTAL including depreciation.
Depreciation is written as a separate ``DEPR`` series (straight-line, 5 years,
from ``investment_plan.csv``). DEPR is a *component* of OTH, not a sixth
category: OTH - DEPR is the regressed part whose true parameters are recorded
in ``true_parameters.json``.

Outputs (``data/illustrative/``):
    actuals.csv            long format: category_code, year, value, source_label
    actuals.xlsx           wide sheet "Istwerten": rows = category, columns = years
    investment_plan.csv    year, capex, depreciation   (2016-2030)
    true_parameters.json   exact alpha, v, beta, sigma and realized noise per category
    bs_mapping.yaml        illustrative P&L -> balance-sheet mapping + opening BS 2025
    external_notes.csv     3-4 dummy notes
    env_framework.yaml     6 domains x 9 positions = 54 scan positions
    control_table.yaml     rules for the AI revenue proposal, tax rate, windows
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from nvplan.config import (
    CATEGORY_CODES,
    DATA_DIR,
    ILLUSTRATIVE_LABEL,
    PLAN_YEARS,
    REGRESSION_WINDOW,
)

SEED = 42
BASE_YEAR = 2016
ACTUAL_YEARS = list(range(2016, 2026))  # 10 years
PLAN_YEAR_LIST = list(range(PLAN_YEARS[0], PLAN_YEARS[1] + 1))
INVESTMENT_YEARS = ACTUAL_YEARS + PLAN_YEAR_LIST  # 2016-2030
DEPRECIATION_YEARS = 5  # straight-line, starting in the capex year

REVENUE_START = 12_000.0
REVENUE_GROWTH_RANGE = (0.05, 0.07)
REVENUE_NOISE_SIGMA = 0.005

# True generating parameters for the regressed cost categories.
# OTH parameters apply to OTH EXCLUDING depreciation.
# NOTE: PLAN.md lists (MAT 400/0.060, EXT 250/0.045, OTH 700/0.030). With those
# values personnel tops out at ~76% of total costs, so the non-personnel
# alpha/beta were scaled down to hit the PDF's "personnel ~80% of costs".
TRUE_PARAMETERS: dict[str, dict[str, float]] = {
    "MAT": {"alpha": 300.0, "v": 0.020, "beta": 0.045, "sigma": 0.015},
    "EXT": {"alpha": 200.0, "v": 0.030, "beta": 0.035, "sigma": 0.020},
    "PERS": {"alpha": 6000.0, "v": 0.028, "beta": 0.300, "sigma": 0.010},
    "OTH": {"alpha": 500.0, "v": 0.025, "beta": 0.025, "sigma": 0.020},
}
COST_CODES = ["MAT", "EXT", "PERS", "OTH"]

CAPEX_START = 350.0
CAPEX_GROWTH = 0.04
CAPEX_JITTER_SIGMA = 0.10

DSO_DAYS = 45
DPO_DAYS = 30
INVENTORY_DAYS = 30
TAX_RATE = 0.25
OPENING_CASH = 2_500.0

CATEGORY_NAMES = {
    "REV": "Revenue",
    "MAT": "Material costs",
    "EXT": "External services",
    "PERS": "Personnel costs",
    "OTH": "Other costs incl. Depreciation",
    "DEPR": "Depreciation (component of OTH)",
}


# --------------------------------------------------------------------------- series


def generate_revenue(rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Smooth growth series: returns (revenue, growth_rates, noise_factors)."""
    n = len(ACTUAL_YEARS)
    growth = rng.uniform(*REVENUE_GROWTH_RANGE, size=n - 1)
    noise = rng.normal(0.0, REVENUE_NOISE_SIGMA, size=n - 1)
    rev = np.empty(n)
    rev[0] = REVENUE_START
    for i in range(1, n):
        rev[i] = rev[i - 1] * (1.0 + growth[i - 1]) * (1.0 + noise[i - 1])
    return rev, growth, noise


def generate_cost(
    rng: np.random.Generator, code: str, revenue: np.ndarray
) -> tuple[np.ndarray, np.ndarray, float]:
    """alpha*(1+v)^(t-t0) + beta*Revenue_t + N(0, sigma*mean_level). Returns (cost, noise, mean_level)."""
    p = TRUE_PARAMETERS[code]
    t = np.array(ACTUAL_YEARS) - BASE_YEAR
    clean = p["alpha"] * (1.0 + p["v"]) ** t + p["beta"] * revenue
    mean_level = float(clean.mean())
    noise = rng.normal(0.0, p["sigma"] * mean_level, size=len(ACTUAL_YEARS))
    return clean + noise, noise, mean_level


def generate_investment_plan(rng: np.random.Generator) -> pd.DataFrame:
    """Capex 2016-2030 with straight-line 5-year depreciation starting in the capex year."""
    n = len(INVESTMENT_YEARS)
    jitter = rng.normal(0.0, CAPEX_JITTER_SIGMA, size=n)
    capex = np.array(
        [CAPEX_START * (1.0 + CAPEX_GROWTH) ** i * (1.0 + jitter[i]) for i in range(n)]
    )
    capex = np.round(capex, 1)
    depreciation = np.zeros(n)
    for i, c in enumerate(capex):
        for k in range(DEPRECIATION_YEARS):
            if i + k < n:
                depreciation[i + k] += c / DEPRECIATION_YEARS
    return pd.DataFrame(
        {"year": INVESTMENT_YEARS, "capex": capex, "depreciation": np.round(depreciation, 3)}
    )


def net_book_value(inv: pd.DataFrame, year: int) -> float:
    """Fixed assets at end of ``year`` = sum(capex) - sum(depreciation) up to and incl. ``year``."""
    upto = inv[inv["year"] <= year]
    return float(upto["capex"].sum() - upto["depreciation"].sum())


# --------------------------------------------------------------------------- builders


def build_dataset(seed: int = SEED) -> dict:
    """Return all generated objects in memory (no I/O)."""
    rng = np.random.default_rng(seed)
    revenue, growth, rev_noise = generate_revenue(rng)

    costs: dict[str, np.ndarray] = {}
    noises: dict[str, np.ndarray] = {}
    mean_levels: dict[str, float] = {}
    for code in COST_CODES:
        costs[code], noises[code], mean_levels[code] = generate_cost(rng, code, revenue)

    inv = generate_investment_plan(rng)
    depr_actual = inv.set_index("year").loc[ACTUAL_YEARS, "depreciation"].to_numpy()

    series = {
        "REV": revenue,
        "MAT": costs["MAT"],
        "EXT": costs["EXT"],
        "PERS": costs["PERS"],
        "OTH": costs["OTH"] + depr_actual,  # total incl. depreciation
        "DEPR": depr_actual,
    }
    series = {k: np.round(v, 3) for k, v in series.items()}

    true_params = {
        "label": ILLUSTRATIVE_LABEL,
        "seed": seed,
        "base_year": BASE_YEAR,
        "years": ACTUAL_YEARS,
        "units": "kEUR",
        "model": "cost_c,t = alpha_c * (1 + v_c)^(t - base_year) + beta_c * revenue_t + noise_c,t; "
        "noise_c,t ~ N(0, sigma_c * mean_level_c)",
        "revenue": {
            "start": REVENUE_START,
            "growth_range": list(REVENUE_GROWTH_RANGE),
            "noise_sigma": REVENUE_NOISE_SIGMA,
            "realized_growth": [float(g) for g in growth],
            "realized_noise": [float(e) for e in rev_noise],
            "values": [float(x) for x in series["REV"]],
        },
        "categories": {
            code: {
                "alpha": TRUE_PARAMETERS[code]["alpha"],
                "v": TRUE_PARAMETERS[code]["v"],
                "beta": TRUE_PARAMETERS[code]["beta"],
                "sigma": TRUE_PARAMETERS[code]["sigma"],
                "mean_level": mean_levels[code],
                "noise_std_abs": TRUE_PARAMETERS[code]["sigma"] * mean_levels[code],
                "realized_noise": [float(x) for x in noises[code]],
                "applies_to": "OTH minus DEPR" if code == "OTH" else code,
            }
            for code in COST_CODES
        },
        "depreciation": {
            "method": f"straight-line over {DEPRECIATION_YEARS} years starting in the capex year",
            "note": "DEPR is a component of OTH; actuals.csv OTH = regressed part + DEPR",
        },
        "regression_window": list(REGRESSION_WINDOW),
        "plan_years": list(PLAN_YEARS),
    }
    return {"series": series, "investment_plan": inv, "true_parameters": true_params}


def actuals_long(series: dict[str, np.ndarray]) -> pd.DataFrame:
    rows = []
    for code in CATEGORY_CODES + ["DEPR"]:
        for year, value in zip(ACTUAL_YEARS, series[code]):
            rows.append(
                {"category_code": code, "year": int(year), "value": float(value), "source_label": ILLUSTRATIVE_LABEL}
            )
    return pd.DataFrame(rows)


def actuals_wide(series: dict[str, np.ndarray]) -> pd.DataFrame:
    codes = CATEGORY_CODES + ["DEPR"]
    wide = pd.DataFrame({year: [series[c][i] for c in codes] for i, year in enumerate(ACTUAL_YEARS)})
    wide.insert(0, "name", [CATEGORY_NAMES[c] for c in codes])
    wide.insert(0, "category_code", codes)
    return wide


def build_bs_mapping(series: dict[str, np.ndarray], inv: pd.DataFrame) -> dict:
    last = ACTUAL_YEARS[-1]
    i = ACTUAL_YEARS.index(last)
    rev, mat, ext = float(series["REV"][i]), float(series["MAT"][i]), float(series["EXT"][i])
    receivables = round(rev * DSO_DAYS / 365, 3)
    inventory = round(mat * INVENTORY_DAYS / 365, 3)
    fixed_assets = round(net_book_value(inv, last), 3)
    payables = round((mat + ext) * DPO_DAYS / 365, 3)
    cash = OPENING_CASH
    total_assets = round(cash + receivables + inventory + fixed_assets, 3)
    equity = round(total_assets - payables, 3)
    return {
        "label": ILLUSTRATIVE_LABEL,
        "description": "Illustrative P&L -> balance-sheet mapping until Newvision supplies its own. "
        "Cash flow = delta of balance-sheet movements.",
        "units": "kEUR",
        "assumptions": {
            "dso_days": DSO_DAYS,
            "dpo_days": DPO_DAYS,
            "inventory_days": INVENTORY_DAYS,
            "days_per_year": 365,
            "depreciation_years": DEPRECIATION_YEARS,
            "tax_rate_ref": "control_table.yaml: tax_rate",
        },
        "pl": {
            "lines": [
                {"code": "revenue", "source": "REV"},
                {"code": "material", "source": "MAT"},
                {"code": "external", "source": "EXT"},
                {"code": "personnel", "source": "PERS"},
                {"code": "other", "source": "OTH", "note": "incl. depreciation (DEPR component)"},
                {"code": "ebit", "formula": "revenue - material - external - personnel - other"},
                {"code": "tax", "formula": "max(ebit, 0) * tax_rate"},
                {"code": "net_income", "formula": "ebit - tax"},
            ]
        },
        "bs": {
            "assets": [
                {"code": "cash", "formula": "balancing: total_liabilities_equity - receivables - inventory - fixed_assets"},
                {"code": "receivables", "formula": "revenue * dso_days / days_per_year"},
                {"code": "inventory", "formula": "material * inventory_days / days_per_year"},
                {"code": "fixed_assets", "formula": "prior_fixed_assets + capex - depreciation"},
            ],
            "liabilities_equity": [
                {"code": "payables", "formula": "(material + external) * dpo_days / days_per_year"},
                {"code": "equity", "formula": "prior_equity + net_income"},
            ],
            "balance_check": "cash + receivables + inventory + fixed_assets == payables + equity",
        },
        "cf": {
            "lines": [
                {"code": "cf_operating", "formula": "net_income + depreciation - delta(receivables) - delta(inventory) + delta(payables)"},
                {"code": "cf_investing", "formula": "-capex"},
                {"code": "cf_financing", "formula": "0"},
                {"code": "delta_cash", "formula": "cf_operating + cf_investing + cf_financing"},
            ],
            "tie_check": "delta_cash == cash_t - cash_{t-1}",
        },
        "opening_balance_sheet": {
            "year": last,
            "note": "End of last actual year; equity set so that the sheet balances at the chosen cash level.",
            "cash": cash,
            "receivables": receivables,
            "inventory": inventory,
            "fixed_assets": fixed_assets,
            "total_assets": total_assets,
            "payables": payables,
            "equity": equity,
        },
    }


def build_external_notes() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "category_code": "REV",
                "year": 2027,
                "text": "Major client contract (approx. 9% of revenue) ends Q2 2027; renewal uncertain.",
                "author": "planning-team",
                "source": "manual",
            },
            {
                "category_code": "REV",
                "year": 2026,
                "text": "New public-sector framework agreement signed; ramp-up expected from H2 2026.",
                "author": "sales",
                "source": "manual",
            },
            {
                "category_code": "PERS",
                "year": 2026,
                "text": "Collective wage agreement: +4.5% from January 2026 (above the valorized default).",
                "author": "hr",
                "source": "manual",
            },
            {
                "category_code": "EXT",
                "year": 2028,
                "text": "Cloud hosting supplier announces price increase of 12% for 2028.",
                "author": "it-ops",
                "source": "manual",
            },
        ]
    )


ENV_DOMAINS: dict[str, list[str]] = {
    "Political": [
        "Government stability", "Public-sector IT budgets", "Trade policy", "Subsidy programmes",
        "Procurement rules", "Digitalisation agenda", "Regional policy", "Tax policy direction", "Geopolitical risk",
    ],
    "Economic": [
        "GDP growth", "Inflation", "Interest rates", "Wage growth", "Exchange rates",
        "Customer-industry cycles", "Software market growth", "Credit availability", "Energy prices",
    ],
    "Social": [
        "Talent availability", "Remote-work expectations", "Demographics", "Customer digital adoption",
        "Education pipeline", "Employer branding", "Work-life expectations", "Diversity expectations", "Consumer trust in software",
    ],
    "Technological": [
        "AI adoption", "Cloud migration", "Cybersecurity threats", "Open-source dynamics", "Platform shifts",
        "Low-code / no-code", "Data regulation tooling", "Legacy system retirement", "R&D productivity",
    ],
    "Environmental": [
        "Energy efficiency requirements", "Carbon reporting duties", "Green procurement criteria", "Data-centre footprint",
        "Climate-related disruption", "Sustainability certifications", "E-waste rules", "Travel policy", "Supplier sustainability",
    ],
    "Legal": [
        "Data protection (GDPR)", "AI regulation (AI Act)", "Labour law", "Contract law changes", "IP and licensing",
        "Product liability for software", "Accessibility requirements", "Export control", "Competition law",
    ],
}


def build_env_framework() -> dict:
    domains = []
    for d_idx, (domain, positions) in enumerate(ENV_DOMAINS.items(), start=1):
        domains.append(
            {
                "id": f"D{d_idx}",
                "name": domain,
                "positions": [
                    {"id": f"D{d_idx}.P{p_idx}", "name": name, "affects": ["REV"] if domain in ("Economic", "Political") else ["REV", "PERS", "EXT"]}
                    for p_idx, name in enumerate(positions, start=1)
                ],
            }
        )
    total = sum(len(d["positions"]) for d in domains)
    assert total == 54, total
    return {
        "label": ILLUSTRATIVE_LABEL,
        "description": "Dummy 54-position environmental scan framework (6 domains x 9 positions, PESTEL-like).",
        "position_count": total,
        "domains": domains,
    }


def build_control_table() -> dict:
    return {
        "label": ILLUSTRATIVE_LABEL,
        "description": "Rules the AI revenue proposal must satisfy plus global planning settings.",
        "revenue_proposal": {
            "max_deviation_from_default_pct": 25,
            "must_cite_note": True,
            "requires_human_confirmation": True,
            "scenario_spread": {"best": 0.08, "worst": -0.08},
            "scenario_spread_note": "relative to the base revenue path",
        },
        "tax_rate": TAX_RATE,
        "regression_window": {"from": REGRESSION_WINDOW[0], "to": REGRESSION_WINDOW[1]},
        "plan_years": {"from": PLAN_YEARS[0], "to": PLAN_YEARS[1]},
        "backtest": {"mape_threshold_pct": {"min": 5, "max": 8}},
    }


# --------------------------------------------------------------------------- I/O


def _yaml_header(title: str) -> str:
    return (
        f"# {title}\n"
        f"# {ILLUSTRATIVE_LABEL}: generated dummy data, not Newvision figures.\n"
        f"# Generated by nvplan.data.generate (seed={SEED}).\n"
    )


def _write_yaml(path: Path, title: str, payload: dict) -> None:
    path.write_text(_yaml_header(title) + yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")


def write_all(out_dir: Path | None = None, seed: int = SEED) -> list[Path]:
    out_dir = Path(out_dir or DATA_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    ds = build_dataset(seed)
    series, inv = ds["series"], ds["investment_plan"]
    written: list[Path] = []

    p = out_dir / "actuals.csv"
    actuals_long(series).to_csv(p, index=False)
    written.append(p)

    p = out_dir / "actuals.xlsx"
    with pd.ExcelWriter(p, engine="openpyxl") as xw:
        actuals_wide(series).to_excel(xw, sheet_name="Istwerten", index=False)
        pd.DataFrame(
            {"note": [f"{ILLUSTRATIVE_LABEL} dummy data, kEUR, generated by nvplan.data.generate seed={seed}",
                      "DEPR is a component of OTH (Other costs incl. Depreciation), not a sixth category."]}
        ).to_excel(xw, sheet_name="README", index=False)
    written.append(p)

    p = out_dir / "investment_plan.csv"
    inv.assign(source_label=ILLUSTRATIVE_LABEL).to_csv(p, index=False)
    written.append(p)

    p = out_dir / "true_parameters.json"
    p.write_text(json.dumps(ds["true_parameters"], indent=2), encoding="utf-8")
    written.append(p)

    p = out_dir / "bs_mapping.yaml"
    _write_yaml(p, "Illustrative P&L -> Balance Sheet -> Cash Flow mapping", build_bs_mapping(series, inv))
    written.append(p)

    p = out_dir / "external_notes.csv"
    build_external_notes().to_csv(p, index=False)
    written.append(p)

    p = out_dir / "env_framework.yaml"
    _write_yaml(p, "Environmental scan framework (54 positions)", build_env_framework())
    written.append(p)

    p = out_dir / "control_table.yaml"
    _write_yaml(p, "Control table", build_control_table())
    written.append(p)

    return written


def main() -> None:
    files = write_all()
    print(f"[{ILLUSTRATIVE_LABEL}] wrote {len(files)} files at {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    for f in files:
        print("  ", f)


if __name__ == "__main__":
    main()
