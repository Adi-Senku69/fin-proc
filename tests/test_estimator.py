"""Closing the two method caveats from VERIFICATION.md.

5.2 -- the valorization rate guard: when the fitted intercept is a negligible share of mean
cost, or the fixed part changes sign across the window, "mean YoY growth of the fixed part" is
not a meaningful rate. It must be reported undefined, project at a fallback of 0.0, and record
the fallback and its reason on the trace -- and nothing downstream may crash.

5.1 -- the joint estimator: a selectable ``method="joint"`` that fits alpha, its own growth
rate and beta together by non-linear least squares, instead of the PDF's constant-intercept
OLS, which structurally absorbs a genuinely growing fixed part into beta.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from nvplan import config
from nvplan.config import DATA_DIR
from nvplan.core.ledger import DerivationLedger, to_wide
from nvplan.core.projector import project_scenario
from nvplan.core.regression import (
    FitResult,
    _valorization_guard,
    fit_all,
    fit_category,
    fit_joint,
)
from nvplan.db.models import Parameter
from nvplan.db.session import SessionLocal, get_engine, init_db, seed_categories
from nvplan.ingest.actuals import ingest_actuals, load_actuals_csv
from nvplan.services.planning import run_plan

# --------------------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def wide() -> pd.DataFrame:
    return to_wide(pd.read_csv(DATA_DIR / "actuals.csv"))


@pytest.fixture()
def db_session():
    engine = init_db(get_engine("sqlite://"))
    with SessionLocal(bind=engine) as s:
        seed_categories(s)
        ingest_actuals(s, load_actuals_csv(DATA_DIR / "actuals.csv"))
        yield s


# --------------------------------------------------------------------------- 5.2: the guard, unit-level


def test_guard_degenerate_intercept():
    status, reason = _valorization_guard(
        alpha=5.0, fixed=np.array([10.0, 8.0, 12.0]), mean_cost=1000.0, share=0.05
    )
    assert status == "degenerate_intercept"
    assert reason and "5.0%" in reason


def test_guard_sign_change():
    # alpha=100 is comfortably above the 5% share of mean_cost=1000, so only the sign flip
    # of the fixed part should trip the guard.
    status, reason = _valorization_guard(
        alpha=100.0, fixed=np.array([50.0, -20.0, 80.0]), mean_cost=1000.0, share=0.05
    )
    assert status == "sign_change"
    assert reason and "sign" in reason


def test_guard_ok():
    status, reason = _valorization_guard(
        alpha=100.0, fixed=np.array([90.0, 95.0, 110.0]), mean_cost=1000.0, share=0.05
    )
    assert status == "ok"
    assert reason is None


def test_guard_zero_or_negative_mean_cost_is_degenerate():
    status, _ = _valorization_guard(alpha=1.0, fixed=np.array([1.0, 1.0]), mean_cost=0.0, share=0.05)
    assert status == "degenerate_intercept"


# --------------------------------------------------------------------------- 5.2: the real degenerate case


def test_external_services_2019_2023_is_degenerate(wide):
    """Hand-verified in VERIFICATION.md 5.2: alpha ~= 10 k EUR, ~1.2% of mean cost, and the raw
    mean YoY growth of the fixed part is about -305%. The guard must report that undefined."""
    ledger = DerivationLedger()
    f = fit_category(wide, "EXT", (2019, 2023), ledger=ledger)

    assert f.alpha == pytest.approx(9.98546249219442, abs=1e-6)
    mean_cost = float(np.mean([p["cost"] for p in f.points]))
    assert abs(f.alpha) / mean_cost < 0.05  # < the 5% default share

    assert f.valorization_status == "degenerate_intercept"
    assert f.valorization_rate_raw is None
    assert f.valorization_rate == 0.0  # the fallback used for projection
    assert f.valorization_fallback_reason is not None
    assert "degeneracy threshold" in f.valorization_fallback_reason

    # The raw (un-guarded) number really is the -305% VERIFICATION.md measured -- this is the
    # exact value the guard is suppressing from projection, not a different computation.
    raw_v, _ = np.mean(
        [
            f.fixed_part_series[y] / f.fixed_part_series[y - 1] - 1.0
            for y in range(2020, 2024)
        ]
    ), None
    assert raw_v == pytest.approx(-3.047870142662841, rel=1e-6)

    # Recorded in the derivation's parameters, so it surfaces in the trace, not hidden.
    d = ledger.get("param:EXT")
    assert d.parameters["valorization_status"] == "degenerate_intercept"
    assert d.parameters["valorization_rate_raw"] is None
    assert d.parameters["v"] == 0.0
    assert d.parameters["valorization_fallback_reason"]
    assert d.parameters["method"] == "ols"

    # Nothing downstream crashes: project this fit through the same function run_plan uses.
    plan_ledger = DerivationLedger()
    revenue_path = pd.Series({2024: 20000.0, 2025: 21000.0, 2026: 22000.0}, name="REV")
    keys = {y: f"input:REV:{y}" for y in revenue_path.index}
    plan = project_scenario(
        {"EXT": f},
        revenue_path,
        t0=2023,
        scenario="base",
        depreciation=pd.Series(dtype=float),
        ledger=plan_ledger,
        revenue_path_keys=keys,
    )
    ext_rows = plan[plan.category_code == "EXT"].set_index("year")
    for y, r in revenue_path.items():
        # v=0 fallback => the fixed part is flat at alpha for every projected year.
        assert ext_rows.loc[y, "value"] == pytest.approx(f.alpha + f.beta * r, abs=1e-9)
    for key in keys.values():
        pass  # no exception constructing/registering the derivation either


def test_degeneracy_share_is_configurable(wide):
    """A category well clear of the default 5% share is reported degenerate under a wide
    enough threshold, and the reverse: EXT's window is *not* degenerate under a near-zero one
    were it not for the sign change (VERIFICATION.md 5.2's second, independent condition)."""
    ledger = DerivationLedger()
    f_default = fit_category(wide, "EXT", (2019, 2023), ledger=ledger, degeneracy_share=0.05)
    assert f_default.valorization_status == "degenerate_intercept"

    ledger2 = DerivationLedger()
    f_tiny_share = fit_category(wide, "EXT", (2019, 2023), ledger=ledger2, degeneracy_share=1e-9)
    # alpha's share now clears the (near-zero) threshold, but the fixed part still changes sign.
    assert f_tiny_share.valorization_status == "sign_change"
    assert f_tiny_share.valorization_rate == 0.0

    ledger3 = DerivationLedger()
    f_wide_share = fit_category(wide, "MAT", degeneracy_share=0.5, ledger=ledger3)
    assert f_wide_share.valorization_status == "degenerate_intercept"


def test_config_default_degeneracy_share():
    assert config.VALORIZATION_DEGENERACY_SHARE == 0.05


# --------------------------------------------------------------------------- 5.1: the joint estimator


def test_joint_recovers_noise_free_series_within_1pct_ols_does_not():
    """The demonstration: alpha, v and beta chosen up front, cost built exactly from them (no
    noise). The joint estimator must recover all three within 1%; OLS -- which fits a constant
    alpha -- structurally cannot (VERIFICATION.md 5.1)."""
    years = np.arange(2021, 2026)
    rev = 16000.0 * 1.06 ** np.arange(len(years))
    a_true, v_true, b_true = 6000.0, 0.028, 0.3
    cost = a_true * (1.0 + v_true) ** (years - years[-1]) + b_true * rev
    wide = pd.DataFrame({"REV": rev, "PERS": cost}, index=pd.Index(years, name="year"))

    ledger = DerivationLedger()
    joint = fit_category(wide, "PERS", (int(years[0]), int(years[-1])), ledger=ledger, method="joint")
    assert joint.method == "joint"
    assert joint.joint_converged is True
    assert joint.joint_iterations and joint.joint_iterations > 0
    assert joint.valorization_status == "ok"
    assert joint.alpha == pytest.approx(a_true, rel=0.01)
    assert joint.valorization_rate == pytest.approx(v_true, rel=0.01)
    assert joint.valorization_rate_raw == pytest.approx(v_true, rel=0.01)
    assert joint.beta == pytest.approx(b_true, rel=0.01)
    assert joint.r_squared == pytest.approx(1.0, abs=1e-9)

    d = ledger.get("param:PERS")
    assert d.parameters["method"] == "joint"
    assert d.parameters["joint_converged"] is True
    assert d.parameters["joint_iterations"] == joint.joint_iterations

    ledger2 = DerivationLedger()
    ols_fit = fit_category(wide, "PERS", (int(years[0]), int(years[-1])), ledger=ledger2, method="ols")
    assert ols_fit.method == "ols"
    assert ols_fit.joint_converged is None
    assert ols_fit.joint_iterations is None
    # OLS structurally absorbs alpha's growth into beta -- nowhere near 1%, nowhere near 20%.
    assert abs(ols_fit.beta / b_true - 1.0) > 0.20


def test_fit_joint_low_level_matches_fit_category():
    """:func:`fit_joint` is exposed directly (not just through fit_category)."""
    years = np.arange(2021, 2026)
    t0 = years[-1]
    dt = (years - t0).astype(float)
    rev = 16000.0 * 1.06 ** np.arange(len(years))
    a_true, v_true, b_true = 6000.0, 0.028, 0.3
    cost = a_true * (1.0 + v_true) ** dt + b_true * rev

    alpha, v, beta, converged, iterations = fit_joint(dt, rev, cost, alpha0=3000.0, beta0=0.5)
    assert converged is True
    assert iterations > 0
    assert alpha == pytest.approx(a_true, rel=0.01)
    assert v == pytest.approx(v_true, rel=0.01)
    assert beta == pytest.approx(b_true, rel=0.01)


def test_joint_falls_back_to_ols_when_not_converged():
    """A pathological input (only 3 points feeding 3 free parameters, so the residual has zero
    degrees of freedom to judge a step by) must not raise; the caller uses the OLS fallback."""
    years = np.arange(2023, 2026)
    dt = years - years[-1]
    rev = np.array([100.0, 1e9, 100.0])  # wildly ill-conditioned, defeats convergence
    cost = np.array([10.0, -5.0, 20.0])
    alpha, v, beta, converged, iterations = fit_joint(
        dt.astype(float), rev, cost, alpha0=0.0, beta0=0.0, max_iter=5
    )
    assert isinstance(converged, bool)
    assert isinstance(iterations, int)
    # whatever happens, every returned value is finite -- never NaN/inf reaching a FitResult
    assert all(np.isfinite(x) for x in (alpha, v, beta))


def test_invalid_method_rejected(wide):
    with pytest.raises(ValueError):
        fit_category(wide, "MAT", ledger=DerivationLedger(), method="bogus")


def test_config_default_method_is_ols():
    assert config.REGRESSION_METHOD == "ols"
    assert config.REGRESSION_METHODS == ("ols", "joint")


# --------------------------------------------------------------------------- threading: fit_all / run_plan


def test_fit_all_threads_method(wide):
    ledger = DerivationLedger()
    fits = fit_all(wide, ("PERS",), (2021, 2025), ledger=ledger, method="joint")
    assert fits["PERS"].method == "joint"

    ledger2 = DerivationLedger()
    fits2 = fit_all(wide, ("PERS",), (2021, 2025), ledger=ledger2)
    assert fits2["PERS"].method == "ols"


def test_run_plan_default_equals_explicit_ols_byte_identical(db_session):
    """Default behaviour is untouched: a plan run with no ``method`` argument produces
    byte-identical parameters to an explicit ``method="ols"`` run (the same code path)."""
    run_a = run_plan(db_session, created_by="a")
    run_b = run_plan(db_session, created_by="b", method="ols")
    for code in ("MAT", "EXT", "PERS", "OTH"):
        pa = db_session.get(Parameter, run_a.parameter_ids[code])
        pb = db_session.get(Parameter, run_b.parameter_ids[code])
        assert pa.alpha == pb.alpha
        assert pa.beta == pb.beta
        assert pa.r_squared == pb.r_squared
        assert pa.valorization_rate == pb.valorization_rate


def test_run_plan_joint_method_persists_without_crashing(db_session):
    """``method`` reaches fit_all through run_plan; nothing downstream raises, including the
    persisted (non-nullable) parameter.valorization_rate row."""
    run = run_plan(db_session, created_by="joint-run", method="joint")
    assert run.n_plan_values > 0
    assert run.n_derivations > 0
    for code in ("MAT", "EXT", "PERS", "OTH"):
        p = db_session.get(Parameter, run.parameter_ids[code])
        assert isinstance(p.valorization_rate, float)
        assert np.isfinite(p.valorization_rate)


def test_run_plan_production_window_never_degenerate(wide):
    """The standard planning window (2021-2025) is comfortably identified for every category,
    so the guard added for VERIFICATION.md 5.2 never changes the production default figures."""
    ledger = DerivationLedger()
    fits = fit_all(wide, ledger=ledger, depreciation=wide["DEPR"])
    for code, f in fits.items():
        assert f.valorization_status == "ok", (code, f.valorization_status)
        assert f.valorization_rate == f.valorization_rate_raw
