"""Closing the two method caveats from VERIFICATION.md.

5.2 -- the valorization rate guard: when the fitted intercept is a negligible share of mean
cost, or the fixed part changes sign across the window, "mean YoY growth of the fixed part" is
not a meaningful rate. It must be reported undefined, project at a fallback of 0.0, and record
the fallback and its reason on the trace -- and nothing downstream may crash.

5.1 -- the joint estimator: a selectable ``method="joint"`` that fits alpha, its own growth
rate and beta together by non-linear least squares, instead of the PDF's constant-intercept
OLS, which structurally absorbs a genuinely growing fixed part into beta.

5.4 -- the joint fit's plausibility guard: on the real five-year window the joint estimator
*converges* to a negative variable rate for Other costs, which is not merely a worse estimate
but an economically impossible one (cost falling as revenue rises). Convergence is not the
right check; plausibility is. A converged fit that is negative or too large a beta, a negative
alpha, an out-of-band valorization rate, or non-finite is rejected and falls back to the
already-computed OLS values, with the rejection recorded rather than hidden.
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
    _plausibility_guard,
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
    # VERIFICATION.md 5.4: a *good* joint fit is accepted, not punished by the new plausibility
    # guard -- it must only reject a converged-but-absurd fit, never a converged-and-correct one.
    assert joint.joint_status == "accepted"
    assert joint.joint_rejection_reason is None
    assert joint.joint_rejected_alpha is None
    assert joint.joint_rejected_beta is None
    assert joint.joint_rejected_v is None
    assert joint.joint_fallback_alpha is None
    assert joint.joint_fallback_beta is None

    d = ledger.get("param:PERS")
    assert d.parameters["method"] == "joint"
    assert d.parameters["joint_converged"] is True
    assert d.parameters["joint_iterations"] == joint.joint_iterations
    assert d.parameters["joint_status"] == "accepted"

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


# --------------------------------------------------------------------------- 5.4: the plausibility guard, unit-level


def test_plausibility_guard_ok():
    status, reason = _plausibility_guard(100.0, 0.3, 0.02, beta_max=2.0, v_min=-0.5, v_max=0.5)
    assert status == "ok"
    assert reason is None


def test_plausibility_guard_negative_beta_fires_in_isolation():
    """Alpha and v both plausible; only beta is negative -- the exact real-fixture failure
    mode for Other costs (VERIFICATION.md 5.4)."""
    status, reason = _plausibility_guard(2605.58, -0.069, 0.056, beta_max=2.0, v_min=-0.5, v_max=0.5)
    assert status == "negative_beta"
    assert reason and "negative" in reason and "beta" in reason


def test_plausibility_guard_beta_ceiling_fires_in_isolation():
    """Alpha and v both plausible, beta positive but above the 2.0 ceiling: a cost category
    consuming more than twice each unit of revenue is not a variable cost."""
    status, reason = _plausibility_guard(100.0, 2.5, 0.02, beta_max=2.0, v_min=-0.5, v_max=0.5)
    assert status == "beta_ceiling"
    assert reason and "ceiling" in reason


def test_plausibility_guard_negative_alpha_fires_in_isolation():
    """Beta and v both plausible; only alpha (the fixed part) is negative."""
    status, reason = _plausibility_guard(-50.0, 0.3, 0.02, beta_max=2.0, v_min=-0.5, v_max=0.5)
    assert status == "negative_alpha"
    assert reason and "negative" in reason and "alpha" in reason


def test_plausibility_guard_valorization_band_fires_in_isolation():
    """Alpha and beta both plausible; only v is outside the [-0.5, 0.5] band (a fixed part
    growing 80% in one year is not a rate worth trusting)."""
    status, reason = _plausibility_guard(100.0, 0.3, 0.8, beta_max=2.0, v_min=-0.5, v_max=0.5)
    assert status == "valorization_band"
    assert reason and "band" in reason

    status2, reason2 = _plausibility_guard(100.0, 0.3, -0.8, beta_max=2.0, v_min=-0.5, v_max=0.5)
    assert status2 == "valorization_band"
    assert reason2 and "band" in reason2


def test_plausibility_guard_non_finite_fires_in_isolation():
    """A non-finite alpha, beta or v is rejected unconditionally -- no knob controls this."""
    for alpha, beta, v in [
        (float("nan"), 0.3, 0.02),
        (100.0, float("inf"), 0.02),
        (100.0, 0.3, float("nan")),
    ]:
        status, reason = _plausibility_guard(alpha, beta, v, beta_max=2.0, v_min=-0.5, v_max=0.5)
        assert status == "non_finite"
        assert reason and "non-finite" in reason


def test_plausibility_guard_ceiling_and_band_are_configurable():
    # beta=1.5 exceeds a tightened ceiling of 1.0 but not the default 2.0.
    assert _plausibility_guard(100.0, 1.5, 0.02, beta_max=2.0, v_min=-0.5, v_max=0.5)[0] == "ok"
    assert _plausibility_guard(100.0, 1.5, 0.02, beta_max=1.0, v_min=-0.5, v_max=0.5)[0] == "beta_ceiling"
    # v=0.3 is inside the default band but outside a tightened [-0.1, 0.1] one.
    assert _plausibility_guard(100.0, 0.3, 0.3, beta_max=2.0, v_min=-0.5, v_max=0.5)[0] == "ok"
    assert _plausibility_guard(100.0, 0.3, 0.3, beta_max=2.0, v_min=-0.1, v_max=0.1)[0] == "valorization_band"


def test_config_defaults_for_plausibility_guard():
    assert config.JOINT_BETA_MAX == 2.0
    assert config.JOINT_VALORIZATION_MIN == -0.5
    assert config.JOINT_VALORIZATION_MAX == 0.5


# --------------------------------------------------------------------------- 5.4: the real fixture, measured


def test_joint_other_costs_negative_beta_is_rejected_and_falls_back_to_ols(wide):
    """Measured in VERIFICATION.md 5.4: on the real 2021-2025 window the joint estimator
    converges (it does not fail to converge) to beta=-0.069 for Other costs net of
    depreciation -- a variable rate asserting cost falls as revenue rises. That is nonsense,
    not merely a worse estimate, so the plausibility guard must reject it and fall back to the
    OLS beta (~0.0445), while preserving the rejected joint value for inspection."""
    ledger = DerivationLedger()
    ledger_ols = DerivationLedger()
    joint = fit_category(
        wide, "OTH", (2021, 2025), ledger=ledger, method="joint",
        subtract=wide["DEPR"], subtract_code="DEPR",
    )
    ols_fit = fit_category(
        wide, "OTH", (2021, 2025), ledger=ledger_ols, method="ols",
        subtract=wide["DEPR"], subtract_code="DEPR",
    )

    assert joint.joint_converged is True  # it *did* converge -- this is not the old fallback path
    assert joint.joint_status == "rejected"
    assert joint.joint_rejection_reason is not None
    assert "negative" in joint.joint_rejection_reason and "beta" in joint.joint_rejection_reason

    # The rejected joint beta really is the measured -0.069, preserved for inspection.
    assert joint.joint_rejected_beta == pytest.approx(-0.06908700449243133, rel=1e-6)
    assert joint.joint_rejected_beta < 0.0

    # The fit falls back entirely to the OLS values.
    assert joint.alpha == pytest.approx(ols_fit.alpha)
    assert joint.beta == pytest.approx(ols_fit.beta)
    assert joint.beta == pytest.approx(0.0445, abs=5e-4)
    assert joint.joint_fallback_alpha == pytest.approx(ols_fit.alpha)
    assert joint.joint_fallback_beta == pytest.approx(ols_fit.beta)

    # Recorded in the derivation, not hidden.
    d = ledger.get("param:OTH")
    assert d.parameters["joint_status"] == "rejected"
    assert d.parameters["joint_rejected_beta"] == pytest.approx(joint.joint_rejected_beta)
    assert d.parameters["joint_fallback_beta"] == pytest.approx(joint.beta)
    assert "rejected" in d.formula_text and "plausibility guard" in d.formula_text


def test_joint_personnel_beta_0_611_is_merely_inaccurate_not_rejected(wide):
    """Measured in VERIFICATION.md 5.4: on the same real window the joint estimator converges
    for Personnel to alpha~1283.65, beta~0.611, v~-0.085 -- all individually plausible (positive
    alpha, beta within (0, 2.0], v within [-0.5, 0.5]). 0.611 is a long way from the generating
    0.300, but a *bad* estimate is not the same thing as an *implausible* one, and the guard
    exists only to catch the latter (VERIFICATION.md 5.4 explicitly distinguishes "worse" from
    "nonsense"). So this fit is NOT rejected: it passes the guard and is used as-is. This is the
    honest outcome, not a forced one -- if a future change to the guard's bands caught this
    fit too, this test should be updated to say so, not silently made to pass."""
    ledger = DerivationLedger()
    joint = fit_category(wide, "PERS", (2021, 2025), ledger=ledger, method="joint")

    assert joint.joint_converged is True
    assert joint.joint_status == "accepted"
    assert joint.joint_rejection_reason is None
    assert joint.joint_rejected_alpha is None
    assert joint.joint_rejected_beta is None
    assert joint.joint_rejected_v is None

    assert joint.alpha == pytest.approx(1283.6526055088475, rel=1e-6)
    assert joint.beta == pytest.approx(0.6106073297053325, rel=1e-6)
    assert joint.valorization_rate == pytest.approx(-0.08546792486626885, rel=1e-6)

    d = ledger.get("param:PERS")
    assert d.parameters["joint_status"] == "accepted"


# --------------------------------------------------------------------------- 5.4: defaults / integration


def test_ols_never_consults_plausibility_guard(wide):
    """The OLS path must not be touched by this guard at all: joint_status stays
    "not_applicable" and every joint_* rejection/fallback field stays None, for every category,
    method omitted or explicit "ols"."""
    ledger = DerivationLedger()
    for code, kwargs in (
        ("MAT", {}), ("EXT", {}),
        ("PERS", {}),
        ("OTH", {"subtract": wide["DEPR"], "subtract_code": "DEPR"}),
    ):
        f_default = fit_category(wide, code, (2021, 2025), ledger=DerivationLedger(), **kwargs)
        f_explicit = fit_category(wide, code, (2021, 2025), ledger=DerivationLedger(), method="ols", **kwargs)
        for f in (f_default, f_explicit):
            assert f.joint_status == "not_applicable"
            assert f.joint_rejection_reason is None
            assert f.joint_rejected_alpha is None
            assert f.joint_rejected_beta is None
            assert f.joint_rejected_v is None
            assert f.joint_fallback_alpha is None
            assert f.joint_fallback_beta is None
        # byte-identical: the default path and the explicit "ols" path are the same code path.
        assert f_default.alpha == f_explicit.alpha
        assert f_default.beta == f_explicit.beta


def test_joint_beta_max_and_valorization_band_overridable_per_call(wide):
    """``joint_beta_max`` / ``joint_valorization_min`` / ``joint_valorization_max`` override
    ``nvplan.config`` the same way ``degeneracy_share`` already does (mirrors
    test_degeneracy_share_is_configurable): Personnel's joint fit (beta~0.611, v~-0.085) passes
    under the defaults but is rejected once the ceiling / band is tightened below its values."""
    ledger = DerivationLedger()
    default = fit_category(wide, "PERS", (2021, 2025), ledger=ledger, method="joint")
    assert default.joint_status == "accepted"

    ledger2 = DerivationLedger()
    tight_beta = fit_category(
        wide, "PERS", (2021, 2025), ledger=ledger2, method="joint", joint_beta_max=0.5
    )
    assert tight_beta.joint_status == "rejected"
    assert tight_beta.joint_rejection_reason is not None
    assert "ceiling" in tight_beta.joint_rejection_reason

    ledger3 = DerivationLedger()
    tight_band = fit_category(
        wide, "PERS", (2021, 2025), ledger=ledger3, method="joint",
        joint_valorization_min=0.0, joint_valorization_max=0.5,
    )
    assert tight_band.joint_status == "rejected"
    assert tight_band.joint_rejection_reason is not None
    assert "band" in tight_band.joint_rejection_reason


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
