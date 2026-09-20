"""Backtest: rolling windows on the illustrative data, verdict logic, noise-free sanity."""

from __future__ import annotations

import dataclasses
import json

import numpy as np
import pandas as pd
import pytest
import yaml

from nvplan.config import DATA_DIR
from nvplan.core import DerivationLedger, to_wide
from nvplan.core.backtest import (
    ERROR_COLUMNS,
    SUMMARY_COLUMNS,
    THRESHOLD_HIGH,
    THRESHOLD_LOW,
    BacktestCase,
    BacktestResult,
    backtest_key,
    default_cases,
    run_backtest,
    summarize_errors,
    verdict,
)
from nvplan.core.regression import fit_all

COST_CODES = ("MAT", "EXT", "PERS", "OTH")
CATEGORIES = ("REV", *COST_CODES)


@pytest.fixture(scope="module")
def wide() -> pd.DataFrame:
    return to_wide(pd.read_csv(DATA_DIR / "actuals.csv"))


@pytest.fixture(scope="module")
def backtest(wide):
    ledger = DerivationLedger()
    result = run_backtest(wide, ledger=ledger, depreciation=wide["DEPR"])
    return ledger, result


# --------------------------------------------------------------------------- cases


def test_default_cases_rolling_windows():
    cases = default_cases(range(2016, 2026), window_len=5, horizons=(1, 2, 3))
    assert len(cases) == 12
    assert cases[0] == BacktestCase((2016, 2020), 2021, 1)
    assert cases[2] == BacktestCase((2016, 2020), 2023, 3)
    assert cases[3] == BacktestCase((2017, 2021), 2022, 1)
    assert cases[-1] == BacktestCase((2020, 2024), 2025, 1)
    assert sorted({c.train_window for c in cases}) == [(2016, 2020), (2017, 2021), (2018, 2022), (2019, 2023), (2020, 2024)]
    assert {h: sum(c.horizon == h for c in cases) for h in (1, 2, 3)} == {1: 5, 2: 4, 3: 3}
    for c in cases:
        assert c.target_year == c.train_window[1] + c.horizon <= 2025
    with pytest.raises(ValueError):
        BacktestCase((2016, 2020), 2022, 1)  # target must equal window end + horizon
    with pytest.raises(ValueError):
        default_cases([2016, 2017, 2019], 3)


# --------------------------------------------------------------------------- illustrative data


def test_backtest_thresholds_match_control_table():
    ct = yaml.safe_load((DATA_DIR / "control_table.yaml").read_text())
    assert ct["backtest"]["mape_threshold_pct"] == {"min": 5, "max": 8}
    assert THRESHOLD_LOW == 0.05 and THRESHOLD_HIGH == 0.08


def test_backtest_illustrative_runs_and_reports(backtest, capsys):
    ledger, result = backtest
    assert isinstance(result, BacktestResult)
    assert len(result.cases) == 12
    assert list(result.errors.columns) == ERROR_COLUMNS
    assert list(result.summary.columns) == SUMMARY_COLUMNS

    # 5 categories x 3 horizons: REV via the default path, costs given actual revenue
    assert len(result.summary) == 15
    assert set(zip(result.summary.category_code, result.summary.horizon)) == {(c, h) for c in CATEGORIES for h in (1, 2, 3)}
    assert set(result.summary.loc[result.summary.category_code == "REV", "basis"]) == {"default_revenue"}
    assert set(result.summary.loc[result.summary.category_code != "REV", "basis"]) == {"actual_revenue"}
    assert result.summary["n"].tolist() == [5, 4, 3] * 5

    assert np.isfinite(result.summary["mape"]).all()
    assert (result.summary["mape"] >= 0).all()
    assert (result.summary["rmse"] >= 0).all()
    assert (result.summary["threshold_low"] == 0.05).all() and (result.summary["threshold_high"] == 0.08).all()

    # errors: 12 cases x (4 costs given actual rev + 5 categories on default path) = 108 rows
    assert len(result.errors) == 108
    assert np.isfinite(result.errors["abs_pct_error"]).all()
    e = result.errors
    assert (e["error"] == e["plan"] - e["actual"]).all()
    assert e["abs_pct_error"].to_numpy() == pytest.approx((e["error"].abs() / e["actual"].abs()).to_numpy())

    # end-to-end table: costs along the default revenue path
    assert len(result.summary_default_path) == 12
    assert set(result.summary_default_path.basis) == {"default_revenue"}

    report = result.to_markdown()
    with capsys.disabled():
        print("\n" + report + "\n")
    assert "| REV | 1 |" in report and "PERS" in report and "verdict" in report

    # the generator noise on PERS is 1%: a 1-year-ahead cascade given actual revenue must beat 5%
    pers_h1 = result.summary[(result.summary.category_code == "PERS") & (result.summary.horizon == 1)].iloc[0]
    assert pers_h1["mape"] < 0.05, pers_h1


def test_backtest_summary_matches_hand_computation(backtest, wide):
    """MAPE / RMSE of one (category, horizon) recomputed by hand from a fresh fit + projection."""
    _, result = backtest
    code, h = "MAT", 2
    rows = result.errors[(result.errors.category_code == code) & (result.errors.horizon == h)
                         & (result.errors.basis == "actual_revenue")]
    assert len(rows) == 4
    apes, errs = [], []
    for r in rows.itertuples():
        window = (r.train_from, r.train_to)
        fits = fit_all(wide, COST_CODES, window, ledger=DerivationLedger(), depreciation=wide["DEPR"])
        f = fits[code]
        plan = f.alpha * (1 + f.valorization_rate) ** (r.target_year - r.train_to) + f.beta * wide.loc[r.target_year, "REV"]
        actual = wide.loc[r.target_year, code]
        assert r.plan == pytest.approx(plan, rel=1e-12)
        assert r.actual == actual
        apes.append(abs(plan - actual) / abs(actual))
        errs.append(plan - actual)
    s = result.summary[(result.summary.category_code == code) & (result.summary.horizon == h)].iloc[0]
    assert s["mape"] == pytest.approx(np.mean(apes), rel=1e-12)
    assert s["rmse"] == pytest.approx(np.sqrt(np.mean(np.square(errs))), rel=1e-12)
    assert s["n"] == 4


def test_backtest_oth_includes_depreciation_like_projector(backtest, wide):
    _, result = backtest
    r = result.errors[(result.errors.category_code == "OTH") & (result.errors.basis == "actual_revenue")
                      & (result.errors.case == "2016-2020->2021")].iloc[0]
    fits = fit_all(wide, COST_CODES, (2016, 2020), ledger=DerivationLedger(), depreciation=wide["DEPR"])
    f = fits["OTH"]
    assert f.component_code == "DEPR"
    plan = f.alpha * (1 + f.valorization_rate) ** 1 + f.beta * wide.loc[2021, "REV"] + wide.loc[2021, "DEPR"]
    assert r.plan == pytest.approx(plan, rel=1e-12)


def test_backtest_derivations(backtest):
    ledger, result = backtest
    ledger.validate()
    keys = ledger.keys()
    assert len(keys) == 15 + 12
    for r in result.summary.itertuples():
        assert r.derivation_key == backtest_key(r.category_code, r.horizon, r.basis)
        d = ledger.get(r.derivation_key)
        cases = d.inputs["cases"]
        assert len(cases) == r.n
        assert {c["case"] for c in cases} == set(
            result.errors[(result.errors.category_code == r.category_code) & (result.errors.horizon == r.horizon)
                          & (result.errors.basis == r.basis)]["case"])
        assert all({"case", "plan", "actual"} <= set(c) for c in cases)
        assert d.parameters["mape"] == r.mape and d.parameters["verdict"] == r.verdict
        json.dumps(d.inputs), json.dumps(d.parameters)
    assert ledger.get("backtest:PERS:h1").parameters["horizon"] == 1
    assert "backtest:MAT:h1:default_revenue" in ledger


# --------------------------------------------------------------------------- verdict logic


def test_verdict_logic():
    assert verdict(0.0) == "within"
    assert verdict(0.05) == "within"
    assert verdict(0.0500001) == "marginal"
    assert verdict(0.08) == "marginal"
    assert verdict(0.0800001) == "missed"
    assert verdict(0.5) == "missed"
    assert verdict(float("nan")) == "undefined"


def test_summarize_errors_verdicts_from_hand_frame():
    rows = []
    for code, apes in (("A", [0.01, 0.03]), ("B", [0.06, 0.07]), ("C", [0.10, 0.20])):
        for i, ape in enumerate(apes):
            rows.append({"case": f"c{i}", "train_from": 2016, "train_to": 2020, "target_year": 2021 + i,
                         "horizon": 1, "category_code": code, "basis": "actual_revenue",
                         "plan": 100.0 * (1 + ape), "actual": 100.0, "error": 100.0 * ape, "abs_pct_error": ape})
    ledger = DerivationLedger()
    s = summarize_errors(pd.DataFrame(rows), ledger=ledger).set_index("category_code")
    assert s.loc["A", "verdict"] == "within" and s.loc["A", "mape"] == pytest.approx(0.02)
    assert s.loc["B", "verdict"] == "marginal" and s.loc["B", "mape"] == pytest.approx(0.065)
    assert s.loc["C", "verdict"] == "missed" and s.loc["C", "mape"] == pytest.approx(0.15)
    assert s.loc["C", "rmse"] == pytest.approx(np.sqrt((10.0**2 + 20.0**2) / 2))
    assert ledger.get("backtest:C:h1").parameters["verdict"] == "missed"


# --------------------------------------------------------------------------- noise-free sanity


def _synthetic(v: float, g: float = 0.06, years=range(2016, 2026)) -> pd.DataFrame:
    ys = np.array(list(years))
    rev = 12000.0 * (1 + g) ** (ys - ys[0])
    depr = 50.0 + 3.0 * np.arange(len(ys))
    true = {"MAT": (300.0, 0.045), "EXT": (200.0, 0.035), "PERS": (6000.0, 0.3), "OTH": (500.0, 0.025)}
    cols = {"REV": rev}
    for code, (a, b) in true.items():
        cols[code] = a * (1 + v) ** (ys - ys[0]) + b * rev
    cols["OTH"] = cols["OTH"] + depr
    cols["DEPR"] = depr
    return pd.DataFrame(cols, index=pd.Index(ys, name="year"))


def test_noise_free_v_zero_backtests_exactly():
    """With v = 0 and no noise OLS recovers alpha, beta exactly -> cost MAPE ~ 0 given actual revenue.

    Revenue grows at a constant g, so the valorized default path is exact too.
    """
    wide = _synthetic(v=0.0)
    ledger = DerivationLedger()
    result = run_backtest(wide, ledger=ledger)
    assert len(result.summary) == 15
    assert (result.summary["mape"] < 1e-9).all(), result.summary
    assert (result.summary["rmse"] < 1e-6).all()
    assert (result.summary["verdict"] == "within").all()
    assert (result.summary_default_path["mape"] < 1e-9).all()
    ledger.validate()


def test_noise_free_v_nonzero_is_not_exact():
    """A drifting fixed part (v != 0) is mis-identified by a constant-intercept OLS: MAPE > 0 even without noise."""
    wide = _synthetic(v=0.03)
    result = run_backtest(wide, ledger=DerivationLedger())
    costs = result.summary[result.summary.category_code != "REV"]
    assert (costs["mape"] > 1e-6).all(), costs
    rev = result.summary[result.summary.category_code == "REV"]
    assert (rev["mape"] < 1e-9).all()  # revenue path is still exact (constant growth)
    # the error grows with the horizon for the largest fixed component
    pers = costs[costs.category_code == "PERS"].set_index("horizon")["mape"]
    assert pers.loc[1] < pers.loc[2] < pers.loc[3]


def test_explicit_cases_and_validation(wide):
    cases = [BacktestCase((2018, 2022), 2024, 2)]
    result = run_backtest(wide, cases=cases, ledger=DerivationLedger())
    assert len(result.errors) == 9 and result.cases == tuple(cases)
    assert set(result.summary.horizon) == {2} and len(result.summary) == 5
    with pytest.raises(ValueError):
        run_backtest(wide, cases=[BacktestCase((2021, 2025), 2026, 1)], ledger=DerivationLedger())
    with pytest.raises(ValueError):
        run_backtest(wide.drop(columns="DEPR"), ledger=DerivationLedger())


# --------------------------------------------------------------------------- C1: review-graded fits are skipped, not crashed


def test_illustrative_backtest_never_skips_a_category(backtest):
    """On the real illustrative data every window's fit is "good" or "fair" (worst R^2 0.91,
    VERIFICATION.md) -- pins that `skipped` being empty is today's ordinary result, not untested
    because the seed data never reaches "review"."""
    _, result = backtest
    assert result.skipped.empty
    assert "skipped" not in result.to_markdown()


def test_a_review_graded_category_is_skipped_not_crashed():
    """The consumer side of C1's structural refusal: `project_scenario` raises ValueError on a
    "review"-graded fit (see tests/test_core_projector.py), which is correct for the live plan
    but wrong for a backtest whose whole point is to measure fits that may be weak. Build a
    synthetic 10-year ledger with one category (BAD) that is deliberately uncorrelated with
    revenue in every rolling window, so its fit reliably grades "review", and confirm
    `run_backtest` excludes it from `summary` and records it in `skipped` instead of raising."""
    years = list(range(2016, 2026))
    rev = 10_000.0 * 1.05 ** np.arange(10)
    good = 100.0 + 0.05 * rev  # tracks revenue closely: always grades "good"
    bad = np.array([50.0, 200.0, 10.0, 180.0, 30.0, 220.0, 5.0, 190.0, 60.0, 210.0])  # never does
    depr = np.full(10, 20.0)
    wide = pd.DataFrame({"REV": rev, "GOOD": good, "BAD": bad, "DEPR": depr}, index=pd.Index(years, name="year"))

    result = run_backtest(wide, cost_codes=("GOOD", "BAD"), ledger=DerivationLedger(), depreciation=wide["DEPR"])

    assert not result.skipped.empty
    assert set(result.skipped["category_code"]) == {"BAD"}
    assert set(result.skipped["train_window"]) == {f"{w}-{w + 4}" for w in range(2016, 2021)}
    assert (result.skipped["reason"].str.contains("R^2", regex=False)).all()

    assert "BAD" not in set(result.summary["category_code"])
    assert "BAD" not in set(result.summary_default_path["category_code"])
    assert "GOOD" in set(result.summary["category_code"])  # the good category is unaffected

    report = result.to_markdown()
    assert "Categories skipped" in report and "BAD" in report


# --------------------------------------------------------------------------- no aggregate MAPE across categories
#
# The reference brief's own discipline: its backtest reports two distinct error bases and
# deliberately carries no aggregate MAPE field, with a test asserting its absence -- because
# averaging MAPE across categories hides that some of them miss their own threshold while others
# comfortably beat it. Checked here rather than ported outright, because NewVision's backtest
# already has this property: `summarize_errors` groups by (category_code, horizon, basis) -- the
# finest granularity, never blended into one number -- and `BacktestResult` carries no field that
# would average across categories. This test pins that finding so a future change cannot
# reintroduce the exact failure mode the reference brief flags.


def test_backtest_reports_no_aggregate_mape_across_categories():
    assert SUMMARY_COLUMNS[:3] == ["category_code", "horizon", "basis"]
    result_fields = {f.name for f in dataclasses.fields(BacktestResult)}
    assert not {"mape", "overall_mape", "aggregate_mape", "blended_mape"} & result_fields


def test_backtest_summary_never_blends_categories_together(backtest):
    """Every summary row's `mape` is per (category, horizon, basis), computed from that group's
    own cases only -- never a mean taken across categories, which would let a badly-missing
    category hide inside a comfortably-passing one."""
    _, result = backtest
    assert result.summary.groupby(["category_code", "horizon", "basis"]).size().eq(1).all()
    assert len(result.summary) == result.summary[["category_code", "horizon"]].drop_duplicates().shape[0]
