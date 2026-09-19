"""Golden tests for nvplan.core.regression (OLS split, valorization, default revenue)."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from nvplan.config import DATA_DIR, PLAN_YEARS, REGRESSION_WINDOW
from nvplan.core import CALC_VERSION, DerivationLedger, to_long, to_wide
from nvplan.core.regression import (
    FitResult,
    fit_all,
    fit_category,
    ols,
    revenue_default_path,
)

COST_CODES = ("MAT", "EXT", "PERS", "OTH")


@pytest.fixture(scope="module")
def wide() -> pd.DataFrame:
    return to_wide(pd.read_csv(DATA_DIR / "actuals.csv"))


@pytest.fixture(scope="module")
def truth() -> dict:
    return json.loads((DATA_DIR / "true_parameters.json").read_text())


def _window_arrays(wide: pd.DataFrame, code: str, window=REGRESSION_WINDOW):
    years = list(range(window[0], window[1] + 1))
    rev = wide.loc[years, "REV"].to_numpy(float)
    cost = wide.loc[years, code].to_numpy(float)
    if code == "OTH":
        cost = cost - wide.loc[years, "DEPR"].to_numpy(float)
    return years, rev, cost


# --------------------------------------------------------------------------- frames


def test_to_wide_roundtrip(wide):
    assert list(wide.index) == list(range(2016, 2026))
    assert list(wide.columns) == ["REV", "MAT", "EXT", "PERS", "OTH", "DEPR"]
    assert wide.loc[2025, "REV"] == 20765.707
    long = to_long(wide)
    back = to_wide(long)
    pd.testing.assert_frame_equal(back, wide[back.columns])


# --------------------------------------------------------------------------- 1. OLS vs polyfit


@pytest.mark.parametrize("code", COST_CODES)
def test_fit_category_matches_polyfit(wide, code):
    ledger = DerivationLedger()
    years, rev, cost = _window_arrays(wide, code)
    kwargs = {"subtract": wide["DEPR"], "subtract_code": "DEPR"} if code == "OTH" else {}
    fit = fit_category(wide, code, REGRESSION_WINDOW, ledger=ledger, **kwargs)

    slope, intercept = np.polyfit(rev, cost, 1)
    assert fit.alpha == pytest.approx(intercept, abs=1e-9)
    assert fit.beta == pytest.approx(slope, abs=1e-9)

    fitted = intercept + slope * rev
    r2 = 1.0 - np.sum((cost - fitted) ** 2) / np.sum((cost - cost.mean()) ** 2)
    assert fit.r_squared == pytest.approx(r2, abs=1e-12)

    # fixed part and valorization by hand
    fixed = cost - slope * rev
    v = np.mean(fixed[1:] / fixed[:-1] - 1.0)
    assert fit.valorization_rate == pytest.approx(v, abs=1e-12)
    assert [fit.fixed_part_series[y] for y in years] == pytest.approx(list(fixed), abs=1e-9)
    # fixed part == alpha + residual, and residuals sum to zero (intercept included)
    assert sum(p["residual"] for p in fit.points) == pytest.approx(0.0, abs=1e-6)
    for p, f in zip(fit.points, fixed):
        assert p["cost"] - p["fitted"] == pytest.approx(p["residual"], abs=1e-9)
        assert fit.alpha + p["residual"] == pytest.approx(f, abs=1e-9)

    assert (fit.window_from, fit.window_to, fit.n) == (2021, 2025, 5)
    assert fit.derivation_key == f"param:{code}"
    d = ledger.get(f"param:{code}")
    assert d.parameters["alpha"] == fit.alpha and d.parameters["beta"] == fit.beta
    assert d.parameters["calc_version"] == CALC_VERSION == "core-1.0"
    assert [p["year"] for p in d.inputs["points"]] == years
    json.dumps(d.inputs), json.dumps(d.parameters)  # JSON-serialisable
    if code == "OTH":
        assert d.inputs["subtracted_code"] == "DEPR"
        assert fit.component_code == "DEPR"
        assert d.inputs["subtracted"][0]["cost_total"] == wide.loc[2021, "OTH"]
    else:
        assert fit.component_code is None


def test_fit_all_subtracts_depreciation_from_oth(wide):
    ledger = DerivationLedger()
    fits = fit_all(wide, COST_CODES, REGRESSION_WINDOW, ledger=ledger, depreciation=wide["DEPR"])
    assert set(fits) == set(COST_CODES)
    years, rev, cost_net = _window_arrays(wide, "OTH")
    assert [p["cost"] for p in fits["OTH"].points] == pytest.approx(list(cost_net))
    # regressing the total (incl. DEPR) gives different parameters
    l2 = DerivationLedger()
    total = fit_category(wide, "OTH", REGRESSION_WINDOW, ledger=l2)
    assert total.beta != fits["OTH"].beta
    # fit_all falls back to the DEPR column when depreciation is not passed
    l3 = DerivationLedger()
    fits3 = fit_all(wide, ledger=l3)
    assert fits3["OTH"].beta == fits["OTH"].beta
    with pytest.raises(ValueError):
        fit_all(wide.drop(columns="DEPR"), ledger=DerivationLedger())


def test_ols_rejects_degenerate_input():
    with pytest.raises(ValueError):
        ols(np.array([1.0, 2.0]), np.array([1.0, 2.0]))
    with pytest.raises(ValueError):
        fit_category(pd.DataFrame({"REV": [1, 2], "MAT": [1, 2]}, index=[2024, 2025]), "MAT", (2021, 2025),
                     ledger=DerivationLedger())


# --------------------------------------------------------------------------- 2. vs generating parameters


def _identifiable_params(wide, truth, code):
    """What OLS recovers on the *noise-free* generator over the same window.

    The generator grows alpha at v while OLS fits a constant intercept, so the
    drift of alpha is absorbed into beta. These are the parameters the PDF
    method can identify from this data; they differ from the generating ones.
    """
    p = truth["categories"][code]
    years, rev, _ = _window_arrays(wide, code)
    t = np.array(years) - truth["base_year"]
    clean = p["alpha"] * (1.0 + p["v"]) ** t + p["beta"] * rev
    a, b, r2, _, _ = ols(rev, clean)
    return a, b, r2


def test_fit_vs_true_parameters_table(wide, truth, capsys):
    ledger = DerivationLedger()
    fits = fit_all(wide, COST_CODES, REGRESSION_WINDOW, ledger=ledger, depreciation=wide["DEPR"])

    hdr = f"{'code':5} {'true_a':>9} {'true_b':>8} {'true_v':>7} | {'fit_a':>10} {'fit_b':>8} {'fit_v':>8} {'R2':>7} | {'b_bias%':>8} {'ols_b_clean':>11}"
    lines = ["", "Regression vs generating parameters (window 2021-2025, ILLUSTRATIVE data)", hdr, "-" * len(hdr)]
    for code in COST_CODES:
        tp = truth["categories"][code]
        f = fits[code]
        _, b_clean, _ = _identifiable_params(wide, truth, code)
        lines.append(
            f"{code:5} {tp['alpha']:9.1f} {tp['beta']:8.3f} {tp['v']:7.3f} | "
            f"{f.alpha:10.3f} {f.beta:8.4f} {f.valorization_rate:8.4f} {f.r_squared:7.4f} | "
            f"{(f.beta / tp['beta'] - 1) * 100:8.1f} {b_clean:11.4f}"
        )
    lines.append("b_bias% = fitted beta vs generating beta; ols_b_clean = beta OLS finds on the noise-free generator")
    with capsys.disabled():
        print("\n".join(lines))

    for code in COST_CODES:
        f = fits[code]
        assert f.r_squared > 0.95, (code, f.r_squared)
        # Exact reconciliation: fitted = OLS on the noise-free generator + OLS of the
        # realized noise on revenue (true_parameters.json records that noise).
        #   beta_fit  = beta_clean  + sum((r - rbar) * eps) / sum((r - rbar)^2)
        #   alpha_fit = alpha_clean + mean(eps) - (beta_fit - beta_clean) * rbar
        a_clean, b_clean, _ = _identifiable_params(wide, truth, code)
        years, rev, _ = _window_arrays(wide, code)
        all_years = truth["years"]
        eps = np.array([truth["categories"][code]["realized_noise"][all_years.index(y)] for y in years])
        rc = rev - rev.mean()
        b_noise = float(np.sum(rc * eps) / np.sum(rc**2))
        assert f.beta == pytest.approx(b_clean + b_noise, abs=1e-5), (code, f.beta, b_clean, b_noise)
        assert f.alpha == pytest.approx(a_clean + eps.mean() - b_noise * rev.mean(), abs=1e-2), code
        # the deviation from the identifiable beta is a noise effect of ordinary size (< 3 s.e.)
        resid = np.array([p["residual"] for p in f.points])
        se_beta = np.sqrt(np.sum(resid**2) / (len(rev) - 2) / np.sum(rc**2))
        assert abs(f.beta - b_clean) < 3 * se_beta, (code, f.beta, b_clean, se_beta)


@pytest.mark.parametrize(
    "code",
    [
        "MAT",
        "EXT",
        pytest.param("PERS", marks=pytest.mark.xfail(
            strict=True, reason="alpha=6000 drifting at 2.8%/yr over a 5-year window is absorbed into beta "
                                "by a constant-intercept OLS (structural bias ~+60%), not a code defect")),
        pytest.param("OTH", marks=pytest.mark.xfail(
            strict=True, reason="same structural bias as PERS (alpha drift absorbed into beta, ~+75%)")),
    ],
)
def test_beta_within_20pct_of_generating_beta(wide, truth, code):
    ledger = DerivationLedger()
    fits = fit_all(wide, COST_CODES, REGRESSION_WINDOW, ledger=ledger, depreciation=wide["DEPR"])
    assert fits[code].beta == pytest.approx(truth["categories"][code]["beta"], rel=0.20)


@pytest.mark.parametrize(
    "code,alpha_true,v_true,beta_true,sigma,seed",
    [
        ("PERS", 6000.0, 0.028, 0.3, 0.01, 5),
        ("OTH", 500.0, 0.025, 0.025, 0.02, 15),
    ],
)
def test_joint_beta_within_20pct_of_generating_beta_where_ols_fails(
    code, alpha_true, v_true, beta_true, sigma, seed
):
    """The parallel case to the two xfails just above: same structural problem (a genuinely
    valorizing alpha vs. a constant-intercept OLS), same PERS / OTH generating parameters as
    ``true_parameters.json``, but resolved by ``method="joint"`` instead of accepted as bias.

    This does *not* reuse the ``wide``/``truth`` fixture's 5-year window (2021-2025) directly:
    jointly fitting three parameters from five points leaves only two residual degrees of
    freedom, which is itself close to unidentifiable under this dataset's actual noise draw --
    verified by hand (a grid search over v finds the least-squares optimum on that specific
    5-point, 3-parameter problem sitting far from the generating v for both PERS and OTH, not
    just for OLS). VERIFICATION.md 5.1 lists "longer window" as an independent, compatible
    fix; this test combines it with the joint estimator, which is exactly what a joint
    estimator needs to be identifiable in practice. It keeps the same generating alpha / v /
    beta / noise-sigma as the real illustrative categories, just over the full 10-year history
    instead of the 5-year regression window, with a fixed (per-category) seed for
    reproducibility.

    That contrast is the point of the whole exercise: OLS on this data is *not* within 20% of
    the generating beta (asserted below, not xfailed -- it is expected to fail and does), while
    the joint estimator is.
    """
    years = np.arange(2016, 2026)
    rev = 12000.0 * 1.06 ** np.arange(len(years))
    clean = alpha_true * (1.0 + v_true) ** (years - years[0]) + beta_true * rev
    rng = np.random.default_rng(seed)
    noise = rng.normal(0.0, sigma * clean.mean(), len(years))
    cost = clean + noise
    wide_synth = pd.DataFrame({"REV": rev, code: cost}, index=pd.Index(years, name="year"))
    window = (int(years[0]), int(years[-1]))

    joint = fit_category(wide_synth, code, window, ledger=DerivationLedger(), method="joint")
    assert joint.beta == pytest.approx(beta_true, rel=0.20)

    ols_fit = fit_category(wide_synth, code, window, ledger=DerivationLedger(), method="ols")
    assert abs(ols_fit.beta / beta_true - 1.0) > 0.20


# --------------------------------------------------------------------------- 3. noise-free sanity


def test_noise_free_recovery_exact():
    years = list(range(2016, 2026))
    rev = 12000.0 * 1.06 ** np.arange(len(years))
    true = {"MAT": (300.0, 0.045), "EXT": (200.0, 0.035), "PERS": (6000.0, 0.3), "OTH": (500.0, 0.025)}
    depr = 50.0 + 3.0 * np.arange(len(years))
    cols = {"REV": rev}
    for code, (a, b) in true.items():
        cols[code] = a + b * rev  # v = 0, no noise
    cols["OTH"] = cols["OTH"] + depr
    cols["DEPR"] = depr
    wide = pd.DataFrame(cols, index=pd.Index(years, name="year"))

    ledger = DerivationLedger()
    fits = fit_all(wide, ledger=ledger)
    for code, (a, b) in true.items():
        f = fits[code]
        assert f.alpha == pytest.approx(a, abs=1e-8)
        assert f.beta == pytest.approx(b, abs=1e-8)
        assert f.r_squared == pytest.approx(1.0, abs=1e-12)
        assert f.valorization_rate == pytest.approx(0.0, abs=1e-9)


def test_noise_free_recovery_with_valorization_from_fixed_part():
    """With alpha growing at v, the *fixed part* f_t = Cost_t - beta*Rev_t grows at v when beta is known."""
    years = np.arange(2021, 2026)
    rev = 16000.0 * 1.06 ** np.arange(len(years))
    a, v, b = 6000.0, 0.028, 0.3
    cost = a * (1 + v) ** (years - 2021) + b * rev
    fixed = cost - b * rev
    assert np.mean(fixed[1:] / fixed[:-1] - 1) == pytest.approx(v, abs=1e-12)


# --------------------------------------------------------------------------- default revenue path


def test_revenue_default_path(wide):
    ledger = DerivationLedger()
    path = revenue_default_path(wide, REGRESSION_WINDOW, PLAN_YEARS, ledger=ledger)
    years = list(range(REGRESSION_WINDOW[0], REGRESSION_WINDOW[1] + 1))
    rev = wide.loc[years, "REV"].to_numpy(float)
    g = np.mean(rev[1:] / rev[:-1] - 1.0)
    assert list(path.index) == list(range(2026, 2031))
    expected = wide.loc[2025, "REV"]
    for y in range(2026, 2031):
        expected *= 1 + g
        assert path.loc[y] == pytest.approx(expected, rel=1e-12)
        d = ledger.get(f"plan:default:REV:{y}")
        assert d.parameters["g"] == pytest.approx(g, abs=1e-15)
        prev = "actual:REV:2025" if y == 2026 else f"plan:default:REV:{y - 1}"
        assert d.parents == (prev, "param:REV")
    assert ledger.get("actual:REV:2025").inputs["value"] == wide.loc[2025, "REV"]
    assert ledger.get("param:REV").parameters["g"] == pytest.approx(g)
    ledger.validate()
    order = ledger.topo_order()
    assert order.index("plan:default:REV:2026") < order.index("plan:default:REV:2030")
    assert order.index("actual:REV:2025") < order.index("plan:default:REV:2026")


def test_fit_result_helpers(wide):
    ledger = DerivationLedger()
    f = fit_category(wide, "MAT", ledger=ledger)
    assert isinstance(f, FitResult)
    assert f.fixed_part_at(2025) == pytest.approx(f.alpha)
    assert f.fixed_part_at(2027) == pytest.approx(f.alpha * (1 + f.valorization_rate) ** 2)
    row = f.as_parameter_row()
    assert row["calc_version"] == "core-1.0" and row["derivation_key"] == "param:MAT"
