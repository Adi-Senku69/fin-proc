"""Statement builder: hand-computed 3-year case, actuals 2016-2025, plan 2026-2030."""

from __future__ import annotations

import math

import pandas as pd
import pytest
import yaml

from nvplan.config import DATA_DIR
from nvplan.core.statements import (
    BS_LINES,
    CF_LINES,
    PL_LINES,
    LineDerivation,
    build_bs,
    build_cf,
    build_pl,
    build_statements,
    check_consistency,
    derive_opening,
    load_mapping,
)

TOL = 1e-9


def _long(values: dict[str, list[float]], years: list[int]) -> pd.DataFrame:
    rows = [
        {"category_code": code, "year": y, "value": v}
        for code, series in values.items()
        for y, v in zip(years, series)
    ]
    return pd.DataFrame(rows)


def _val(df: pd.DataFrame, line: str, year: int) -> float:
    sel = df[(df["line_code"] == line) & (df["year"] == year)]
    assert len(sel) == 1, f"{line}/{year}: {len(sel)} rows"
    return float(sel["value"].iloc[0])


# --------------------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def mapping() -> dict:
    return load_mapping()


@pytest.fixture(scope="module")
def tax_rate() -> float:
    with open(DATA_DIR / "control_table.yaml", encoding="utf-8") as fh:
        return float(yaml.safe_load(fh)["tax_rate"])


@pytest.fixture(scope="module")
def investment_plan() -> pd.DataFrame:
    return pd.read_csv(DATA_DIR / "investment_plan.csv")


@pytest.fixture(scope="module")
def actuals() -> pd.DataFrame:
    return pd.read_csv(DATA_DIR / "actuals.csv")


# --------------------------------------------------------------------------- hand-made case

# Round-number mapping so every line can be checked by hand: DSO 36.5 days = 10 % of
# revenue, inventory 36.5 days = 10 % of material, DPO 73 days = 20 % of (MAT+EXT).
HAND_MAPPING = {
    "assumptions": {"dso_days": 36.5, "dpo_days": 73, "inventory_days": 36.5, "days_per_year": 365},
    "bs": {}, "cf": {},
}
HAND_YEARS = [2026, 2027, 2028]
HAND_PL = {
    "REV": [1000.0, 1200.0, 1100.0],
    "MAT": [100.0, 120.0, 110.0],
    "EXT": [50.0, 60.0, 55.0],
    "PERS": [600.0, 700.0, 650.0],
    "OTH": [150.0, 160.0, 170.0],  # incl. DEPR
    "DEPR": [40.0, 50.0, 60.0],
}
HAND_CAPEX = pd.DataFrame({"year": HAND_YEARS, "capex": [80.0, 30.0, 100.0]})
HAND_OPENING = {"cash": 500.0, "receivables": 90.0, "inventory": 9.0, "fixed_assets": 300.0,
                "payables": 27.0, "equity": 872.0}  # 500+90+9+300 = 899 = 27 + 872
HAND_TAX = 0.25


def test_hand_computed_three_year_statements():
    pl_long = _long(HAND_PL, HAND_YEARS)
    stmts = build_statements(pl_long, HAND_CAPEX, HAND_MAPPING, scenario="base",
                             tax_rate=HAND_TAX, opening=HAND_OPENING)
    pl, bs, cf = stmts.pl, stmts.bs, stmts.cf

    # ---- P&L 2026: total costs = 100 + 50 + 600 + 150 = 900 (OTH already incl. DEPR)
    #      ebit = 1000 - 900 = 100 ; tax = 25 ; NI = 75
    # ---- P&L 2027: total costs = 120 + 60 + 700 + 160 = 1040 ; ebit = 160 ; tax = 40 ; NI = 120
    # ---- P&L 2028: total costs = 110 + 55 + 650 + 170 = 985 ; ebit = 115 ; tax = 28.75 ; NI = 86.25
    expected_pl = {
        2026: {"revenue": 1000, "material": 100, "external": 50, "personnel": 600, "other": 150,
               "depreciation": 40, "total_costs": 900, "ebit": 100, "tax": 25, "net_income": 75},
        2027: {"revenue": 1200, "material": 120, "external": 60, "personnel": 700, "other": 160,
               "depreciation": 50, "total_costs": 1040, "ebit": 160, "tax": 40, "net_income": 120},
        2028: {"revenue": 1100, "material": 110, "external": 55, "personnel": 650, "other": 170,
               "depreciation": 60, "total_costs": 985, "ebit": 115, "tax": 28.75, "net_income": 86.25},
    }
    for year, lines in expected_pl.items():
        for line, exp in lines.items():
            assert _val(pl, line, year) == pytest.approx(exp, abs=TOL), (line, year)

    # ---- BS (cash from the CF below)
    # 2026: AR = 10% * 1000 = 100 ; Inv = 10% * 100 = 10 ; FA = 300 + 80 - 40 = 340
    #       AP = 20% * (100 + 50) = 30 ; Eq = 872 + 75 = 947
    #       CF: dAR = 100 - 90 = 10 ; dInv = 10 - 9 = 1 ; dAP = 30 - 27 = 3
    #           op = 75 + 40 - 10 - 1 + 3 = 107 ; inv = -80 ; ncf = 27 ; cash = 500 + 27 = 527
    #       TA = 527 + 100 + 10 + 340 = 977 ; TLE = 30 + 947 = 977
    # 2027: AR = 120 ; Inv = 12 ; FA = 340 + 30 - 50 = 320 ; AP = 20% * 180 = 36 ; Eq = 947 + 120 = 1067
    #       dAR = 20 ; dInv = 2 ; dAP = 6 ; op = 120 + 50 - 20 - 2 + 6 = 154 ; ncf = 154 - 30 = 124
    #       cash = 527 + 124 = 651 ; TA = 651 + 120 + 12 + 320 = 1103 ; TLE = 36 + 1067 = 1103
    # 2028: AR = 110 ; Inv = 11 ; FA = 320 + 100 - 60 = 360 ; AP = 20% * 165 = 33 ; Eq = 1067 + 86.25 = 1153.25
    #       dAR = -10 ; dInv = -1 ; dAP = -3 ; op = 86.25 + 60 + 10 + 1 - 3 = 154.25 ; ncf = 54.25
    #       cash = 651 + 54.25 = 705.25 ; TA = 705.25 + 110 + 11 + 360 = 1186.25 ; TLE = 33 + 1153.25 = 1186.25
    expected_bs = {
        2025: {"cash": 500, "receivables": 90, "inventory": 9, "fixed_assets": 300, "total_assets": 899,
               "payables": 27, "equity": 872, "total_liabilities_equity": 899},
        2026: {"cash": 527, "receivables": 100, "inventory": 10, "fixed_assets": 340, "total_assets": 977,
               "payables": 30, "equity": 947, "total_liabilities_equity": 977},
        2027: {"cash": 651, "receivables": 120, "inventory": 12, "fixed_assets": 320, "total_assets": 1103,
               "payables": 36, "equity": 1067, "total_liabilities_equity": 1103},
        2028: {"cash": 705.25, "receivables": 110, "inventory": 11, "fixed_assets": 360, "total_assets": 1186.25,
               "payables": 33, "equity": 1153.25, "total_liabilities_equity": 1186.25},
    }
    for year, lines in expected_bs.items():
        for line, exp in lines.items():
            assert _val(bs, line, year) == pytest.approx(exp, abs=TOL), (line, year)

    expected_cf = {
        2026: {"net_income": 75, "depreciation": 40, "delta_receivables": 10, "delta_inventory": 1,
               "delta_payables": 3, "operating_cf": 107, "investing_cf": -80, "financing_cf": 0,
               "net_cash_flow": 27, "opening_cash": 500, "closing_cash": 527},
        2027: {"net_income": 120, "depreciation": 50, "delta_receivables": 20, "delta_inventory": 2,
               "delta_payables": 6, "operating_cf": 154, "investing_cf": -30, "financing_cf": 0,
               "net_cash_flow": 124, "opening_cash": 527, "closing_cash": 651},
        2028: {"net_income": 86.25, "depreciation": 60, "delta_receivables": -10, "delta_inventory": -1,
               "delta_payables": -3, "operating_cf": 154.25, "investing_cf": -100, "financing_cf": 0,
               "net_cash_flow": 54.25, "opening_cash": 651, "closing_cash": 705.25},
    }
    for year, lines in expected_cf.items():
        for line, exp in lines.items():
            assert _val(cf, line, year) == pytest.approx(exp, abs=TOL), (line, year)

    # complete line sets, no extras
    assert set(pl["line_code"]) == set(PL_LINES)
    assert set(bs["line_code"]) == set(BS_LINES)
    assert set(cf["line_code"]) == set(CF_LINES)
    assert len(pl) == len(PL_LINES) * 3
    assert len(bs) == len(BS_LINES) * 4  # incl. opening year
    assert len(cf) == len(CF_LINES) * 3
    assert check_consistency(stmts) == []


def test_standalone_builders_agree_with_build_statements():
    pl_long = _long(HAND_PL, HAND_YEARS)
    ledger: dict[str, LineDerivation] = {}
    pl = build_pl(pl_long, scenario="base", tax_rate=HAND_TAX, ledger=ledger)
    bs = build_bs(pl, HAND_CAPEX, HAND_MAPPING, scenario="base", opening=HAND_OPENING, ledger=ledger)
    cf = build_cf(pl, bs, HAND_CAPEX, scenario="base", ledger=ledger)
    stmts = build_statements(pl_long, HAND_CAPEX, HAND_MAPPING, scenario="base",
                             tax_rate=HAND_TAX, opening=HAND_OPENING)
    for mine, theirs in ((pl, stmts.pl), (bs, stmts.bs), (cf, stmts.cf)):
        pd.testing.assert_frame_equal(mine.reset_index(drop=True), theirs.reset_index(drop=True))
    assert list(pl.columns) == ["line_code", "year", "value", "derivation_key"]
    assert list(bs.columns) == ["line_code", "year", "value", "mapping_ref", "derivation_key"]
    assert set(ledger) == set(stmts.derivations)
    assert _val(cf, "closing_cash", 2028) == pytest.approx(_val(bs, "cash", 2028), abs=TOL)


def test_tax_is_zero_when_ebit_negative():
    values = dict(HAND_PL)
    values = {**values, "PERS": [1000.0, 700.0, 2000.0]}  # 2026 and 2028 loss-making
    pl = build_pl(_long(values, HAND_YEARS), scenario="base", tax_rate=HAND_TAX)
    assert _val(pl, "ebit", 2026) == pytest.approx(1000 - (100 + 50 + 1000 + 150), abs=TOL)  # -300
    assert _val(pl, "tax", 2026) == 0.0
    assert _val(pl, "net_income", 2026) == pytest.approx(-300.0, abs=TOL)
    assert _val(pl, "tax", 2027) == pytest.approx(40.0, abs=TOL)
    assert _val(pl, "tax", 2028) == 0.0
    assert _val(pl, "net_income", 2028) == _val(pl, "ebit", 2028)


def test_check_consistency_detects_a_broken_sheet():
    stmts = build_statements(_long(HAND_PL, HAND_YEARS), HAND_CAPEX, HAND_MAPPING, scenario="base",
                             tax_rate=HAND_TAX, opening=HAND_OPENING)
    assert check_consistency(stmts) == []
    broken = stmts.bs.copy()
    # push 1.0 of extra cash into 2027 (cash AND its total) without a matching source of funds
    mask = broken["line_code"].isin(["cash", "total_assets"]) & (broken["year"] == 2027)
    broken.loc[mask, "value"] += 1.0
    stmts.bs = broken
    problems = check_consistency(stmts)
    assert any("does not balance" in p and "2027" in p for p in problems)
    assert any("delta cash" in p for p in problems)
    assert any("BS cash" in p for p in problems)


# --------------------------------------------------------------------------- illustrative actuals 2016-2025


def test_actuals_2016_2025_with_derived_opening(actuals, investment_plan, mapping, tax_rate):
    stmts = build_statements(actuals, investment_plan, mapping, scenario="actual",
                             tax_rate=tax_rate, opening=None, opening_cash=2000.0)
    assert sorted(stmts.pl["year"].unique()) == list(range(2016, 2026))
    assert sorted(stmts.bs["year"].unique()) == list(range(2015, 2026))
    assert _val(stmts.bs, "cash", 2015) == 2000.0
    assert _val(stmts.bs, "fixed_assets", 2015) == 0.0  # nothing in the investment plan before 2016
    assert check_consistency(stmts) == []
    # fixed assets at end-2025 equal the investment plan's net book value (capex - depreciation, 2016-2025)
    upto = investment_plan[investment_plan["year"] <= 2025]
    nbv = float(upto["capex"].sum() - upto["depreciation"].sum())
    assert _val(stmts.bs, "fixed_assets", 2025) == pytest.approx(nbv, abs=1e-6)
    assert _val(stmts.bs, "fixed_assets", 2025) == pytest.approx(mapping["opening_balance_sheet"]["fixed_assets"], abs=1e-3)
    # working capital 2025 matches the yaml opening (same formulas, same assumptions)
    ob = mapping["opening_balance_sheet"]
    for line in ("receivables", "inventory", "payables"):
        assert _val(stmts.bs, line, 2025) == pytest.approx(ob[line], abs=1e-3)


def test_derive_opening_balances(actuals, investment_plan, mapping, tax_rate):
    pl = build_pl(actuals, scenario="actual", tax_rate=tax_rate)
    op = derive_opening(pl, investment_plan, mapping, cash=2000.0)
    assert op["year"] == 2015
    assert op["cash"] + op["receivables"] + op["inventory"] + op["fixed_assets"] == pytest.approx(
        op["payables"] + op["equity"], abs=TOL)
    assert op["receivables"] == pytest.approx(12000.0 * 45 / 365, abs=TOL)


# --------------------------------------------------------------------------- synthetic plan 2026-2030


def _synthetic_plan() -> pd.DataFrame:
    years = list(range(2026, 2031))
    rev = [21800.0 * 1.06 ** i for i in range(5)]
    return _long(
        {
            "REV": rev,
            "MAT": [320 * 1.02 ** i + 0.045 * r for i, r in enumerate(rev)],
            "EXT": [210 * 1.03 ** i + 0.035 * r for i, r in enumerate(rev)],
            "PERS": [7900 * 1.028 ** i + 0.30 * r for i, r in enumerate(rev)],
            "OTH": [640 * 1.025 ** i + 0.025 * r + d for i, (r, d) in enumerate(zip(rev, [494.28, 505.1, 503.46, 514.34, 529.4]))],
            "DEPR": [494.28, 505.1, 503.46, 514.34, 529.4],
        },
        years,
    )


@pytest.mark.parametrize("scenario", ["base", "best", "worst"])
def test_plan_2026_2030_with_yaml_opening(investment_plan, mapping, tax_rate, scenario):
    stmts = build_statements(_synthetic_plan(), investment_plan, mapping, scenario=scenario,
                             tax_rate=tax_rate, opening=mapping["opening_balance_sheet"])
    assert check_consistency(stmts) == []
    assert sorted(stmts.bs["year"].unique()) == list(range(2025, 2031))
    ob = mapping["opening_balance_sheet"]
    assert _val(stmts.bs, "cash", 2025) == ob["cash"]
    assert _val(stmts.bs, "equity", 2025) == ob["equity"]
    assert _val(stmts.cf, "opening_cash", 2026) == ob["cash"]

    # every line has a derivation; every parent is a plan: key or a key in the set
    keys = set(stmts.derivations)
    for frame in (stmts.pl, stmts.bs, stmts.cf):
        for k in frame["derivation_key"]:
            assert k in keys, k
            assert k.startswith(f"stmt:{scenario}:")
    plan_parents = set()
    for d in stmts.derivations.values():
        assert isinstance(d, LineDerivation)
        assert d.formula_text
        for p in d.parents:
            if p.startswith("plan:"):
                plan_parents.add(p)
            else:
                assert p in keys, (d.key, p)
    assert f"plan:{scenario}:REV:2026" in plan_parents
    assert f"plan:{scenario}:DEPR:2030" in plan_parents
    # the cash line points at the cash-flow statement, never at a balancing plug
    cash_2027 = stmts.derivations[f"stmt:{scenario}:bs:cash:2027"]
    assert cash_2027.parents == (f"stmt:{scenario}:cf:closing_cash:2027",)
    rec = stmts.derivations[f"stmt:{scenario}:bs:receivables:2026"]
    assert rec.parameters["dso_days"] == 45
    assert rec.parents == (f"stmt:{scenario}:pl:revenue:2026",)


def test_plan_with_wrong_opening_year_is_rejected(investment_plan, mapping, tax_rate):
    with pytest.raises(ValueError, match="opening balance sheet is for"):
        build_statements(_synthetic_plan(), investment_plan, mapping, scenario="base",
                         tax_rate=tax_rate, opening={**mapping["opening_balance_sheet"], "year": 2024})


def test_non_consecutive_years_rejected():
    bad = _long(HAND_PL, [2026, 2027, 2029])
    with pytest.raises(ValueError, match="consecutive"):
        build_pl(bad, scenario="base", tax_rate=HAND_TAX)


def test_missing_capex_year_rejected():
    pl = build_pl(_long(HAND_PL, HAND_YEARS), scenario="base", tax_rate=HAND_TAX)
    with pytest.raises(ValueError, match="capex"):
        build_bs(pl, HAND_CAPEX.iloc[:2], HAND_MAPPING, scenario="base", opening=HAND_OPENING)


def test_load_mapping_has_expected_shape(mapping):
    assert mapping["assumptions"]["dso_days"] == 45
    assert mapping["opening_balance_sheet"]["year"] == 2025
    ob = mapping["opening_balance_sheet"]
    assert math.isclose(ob["cash"] + ob["receivables"] + ob["inventory"] + ob["fixed_assets"],
                        ob["payables"] + ob["equity"], abs_tol=1e-6)
