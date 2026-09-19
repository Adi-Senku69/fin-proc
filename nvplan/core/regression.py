"""OLS split of each cost category into fixed (alpha) and variable (beta) parts.

Method (exactly as the PDF, ``method="ols"``, the default):

    Cost_t = alpha + beta * Revenue_t + eps_t         OLS over the window
    R^2    = 1 - SS_res / SS_tot
    f_t    = Cost_t - beta * Revenue_t                fixed part (= alpha + eps_t)
    v      = mean_t ( f_t / f_{t-1} - 1 )             valorization rate (avg YoY growth of f)

A selectable alternative, ``method="joint"`` (VERIFICATION.md 5.1): the PDF's OLS fits a
*constant* alpha, so over a short window the growth of a genuinely valorizing fixed part is
almost collinear with revenue growth and gets absorbed into beta. The joint estimator instead
fits alpha, its own growth rate v and beta together by non-linear least squares on
``alpha*(1+v)^(t-t0) + beta*Revenue_t``, starting from the OLS solution (v=0) and falling back
to it if the fit does not converge.

Either way, the valorization rate is guarded (VERIFICATION.md 5.2): when the fitted alpha is a
negligible share of mean cost, or the fixed part changes sign across the window, "mean YoY
growth of a number that hovers around zero" is not a rate -- it is reported undefined
(``valorization_status`` != ``"ok"``, ``valorization_rate_raw`` is ``None``) and projection
falls back to a rate of 0.0, with the fallback and its reason recorded in the derivation.

A converged joint fit is additionally checked for *plausibility* (VERIFICATION.md 5.4):
convergence only means the solver found a local optimum, not that the optimum makes economic
sense. On the real five-year window the joint estimator converges to a negative variable rate
for Other costs -- costs falling as revenue rises, which is nonsense, not merely inaccurate.
``_plausibility_guard`` rejects a converged joint fit (negative or too-high beta, negative
alpha, an out-of-band v, or any non-finite value) and falls back to the already-computed OLS
values, exactly as it already falls back when the solver does not converge. The rejection --
which check failed, the rejected joint values, and the OLS values that replaced them -- is
recorded on ``FitResult`` and in the derivation rather than hidden.

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

from nvplan import config
from nvplan.core.ledger import CALC_VERSION, DerivationLedger, calc_version

__all__ = [
    "CALC_VERSION",
    "calc_version",
    "FitResult",
    "fit_category",
    "fit_all",
    "ols",
    "fit_joint",
    "revenue_default_path",
    "revenue_growth_rate",
    "actual_key",
    "param_key",
    "default_revenue_key",
    "DEFAULT_COST_CODES",
    "REGRESSION_METHODS",
]

DEFAULT_COST_CODES: tuple[str, ...] = ("MAT", "EXT", "PERS", "OTH")
DEFAULT_WINDOW: tuple[int, int] = (2021, 2025)
COMPONENT_CODE = "DEPR"  # the component subtracted from OTH before regression
REGRESSION_METHODS: tuple[str, ...] = config.REGRESSION_METHODS  # ("ols", "joint")


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
    #: The rate actually used for projection: 0.0 whenever ``valorization_status`` != "ok"
    #: (VERIFICATION.md 5.2's fallback), otherwise equal to ``valorization_rate_raw``. Every
    #: existing caller (``project_scenario``, ``as_parameter_row``, the persisted ``parameter``
    #: row, ``deviation.py``, ``backtest.py``) reads *this* field and a plain float is what it
    #: has always gotten, so none of them need to change to stay safe.
    valorization_rate: float
    window_from: int
    window_to: int
    n: int
    points: list[dict[str, float]] = field(default_factory=list)
    fixed_part_series: dict[int, float] = field(default_factory=dict)
    derivation_key: str = ""
    #: component series subtracted from the raw cost before the fit (e.g. "DEPR" for OTH)
    component_code: str | None = None
    #: "ols" (default, PDF) or "joint" (VERIFICATION.md 5.1's selectable estimator).
    method: str = "ols"
    #: "ok" | "degenerate_intercept" | "sign_change" (VERIFICATION.md 5.2).
    valorization_status: str = "ok"
    #: The honest, un-guarded rate: mean YoY growth of the fixed part (method "ols") or the
    #: jointly-fitted v (method "joint"). ``None`` exactly when ``valorization_status`` != "ok"
    #: -- the rate is undefined, not a (misleading) number.
    valorization_rate_raw: float | None = None
    #: Human-readable reason the rate was reported undefined and 0.0 substituted; ``None`` when
    #: ``valorization_status`` == "ok".
    valorization_fallback_reason: str | None = None
    #: Only meaningful for method "joint": whether the non-linear fit converged (``None`` for
    #: "ols", or for "joint" when it never ran because of a hard input error).
    joint_converged: bool | None = None
    #: Iterations the joint solver took (``None`` for "ols").
    joint_iterations: int | None = None
    #: "not_applicable" (method "ols"), "accepted" (method "joint", converged and passed the
    #: plausibility guard), "not_converged" (method "joint", solver did not converge, fell back
    #: to OLS), or "rejected" (method "joint", converged but implausible -- VERIFICATION.md 5.4
    #: -- fell back to OLS).
    joint_status: str = "not_applicable"
    #: Which plausibility check failed: "negative_beta" | "beta_ceiling" | "negative_alpha" |
    #: "valorization_band" | "non_finite". ``None`` unless ``joint_status == "rejected"``.
    joint_rejection_reason: str | None = None
    #: The converged-but-implausible (alpha, beta, v) the guard rejected, kept for inspection.
    #: ``None`` unless ``joint_status == "rejected"``.
    joint_rejected_alpha: float | None = None
    joint_rejected_beta: float | None = None
    joint_rejected_v: float | None = None
    #: The OLS (alpha, beta) that replaced the rejected joint fit -- equal to this
    #: ``FitResult``'s own ``.alpha`` / ``.beta`` in that case, recorded explicitly so the trace
    #: does not have to infer it. ``None`` unless ``joint_status == "rejected"``.
    joint_fallback_alpha: float | None = None
    joint_fallback_beta: float | None = None

    def fixed_part_at(self, year: int, t0: int | None = None) -> float:
        """alpha valorized to ``year``: alpha * (1+v)^(year - t0), t0 defaults to window_to.

        Uses the effective (guarded) :attr:`valorization_rate`, never ``None``.
        """
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


# --------------------------------------------------------------------------- valorization guard (VERIFICATION 5.2)


def _valorization_guard(
    alpha: float, fixed: np.ndarray, mean_cost: float, share: float
) -> tuple[str, str | None]:
    """Decide whether a valorization rate is trustworthy.

    Degenerate exactly when (VERIFICATION.md 5.2):

    * the fitted intercept is a negligible share of mean cost (``|alpha| < share * mean_cost``
      -- with a growing-sign, i.e. non-positive, ``mean_cost`` treated as always degenerate), or
    * the fixed-part series changes sign across the window.

    Either condition alone is enough; "mean YoY growth" of a number hovering around, or
    crossing, zero is not a meaningful rate. Returns ``(status, reason)`` with
    ``reason is None`` iff ``status == "ok"``.
    """
    if mean_cost <= 0.0:
        reason = (
            f"mean cost {mean_cost:.4f} is not positive, so no share of it is meaningful: "
            "the fixed part's intercept cannot be judged non-degenerate"
        )
        return "degenerate_intercept", reason
    share_pct = abs(alpha) / mean_cost
    if share_pct < share:
        reason = (
            f"|alpha|={abs(alpha):.4f} is {share_pct * 100:.2f}% of mean cost {mean_cost:.4f}, "
            f"below the {share * 100:.1f}% degeneracy threshold: the fixed part hovers around "
            f"zero, so its mean YoY growth is meaningless"
        )
        return "degenerate_intercept", reason
    signs = np.sign(fixed)
    signs = signs[signs != 0]
    if signs.size >= 2 and not np.all(signs == signs[0]):
        reason = (
            f"the fixed part changes sign across the window ({[round(float(f), 4) for f in fixed]}): "
            "its mean YoY growth alternates sign too and is not a rate"
        )
        return "sign_change", reason
    return "ok", None


# --------------------------------------------------------------------------- plausibility guard (VERIFICATION 5.4)


def _plausibility_guard(
    alpha: float, beta: float, v: float, *, beta_max: float, v_min: float, v_max: float
) -> tuple[str, str | None]:
    """Decide whether a *converged* joint fit is economically plausible.

    Convergence answers "did the solver find a local optimum", not "is the optimum sane": on
    the real five-year window (VERIFICATION.md 5.4) the joint estimator converges to a negative
    variable rate for Other costs, which asserts that cost falls as revenue rises -- not a
    worse estimate, nonsense. Implausible exactly when, checked in this order:

    * alpha, beta or v is not finite, or
    * beta is negative (a variable rate cannot be negative), or exceeds ``beta_max`` (a cost
      category consuming more than ``beta_max`` of each unit of revenue is not a variable
      cost), or
    * alpha is negative (a negative fixed cost), or
    * v falls outside ``[v_min, v_max]`` (a fixed part halving, or growing by half or more, in
      one year is not a rate worth trusting).

    Any one condition is enough. Returns ``(status, reason)`` with ``reason is None`` iff
    ``status == "ok"``. The caller falls back entirely to the OLS solution when
    ``status != "ok"``, exactly as it already does when the joint solver fails to converge.
    """
    if not all(np.isfinite(x) for x in (alpha, beta, v)):
        reason = f"joint fit produced a non-finite value: alpha={alpha}, beta={beta}, v={v}"
        return "non_finite", reason
    if beta < 0.0:
        reason = (
            f"beta={beta:.6f} is negative: a variable rate asserts that cost falls as revenue "
            "rises, which is not a plausible variable cost"
        )
        return "negative_beta", reason
    if beta > beta_max:
        reason = (
            f"beta={beta:.6f} exceeds the ceiling {beta_max:.4f}: a cost category consuming "
            f"more than {beta_max:.2f}x each unit of revenue is not a variable cost"
        )
        return "beta_ceiling", reason
    if alpha < 0.0:
        reason = f"alpha={alpha:.6f} is negative: a fixed cost cannot be negative"
        return "negative_alpha", reason
    if not (v_min <= v <= v_max):
        reason = (
            f"v={v:.6f} is outside the plausible band [{v_min:.4f}, {v_max:.4f}]: a fixed part "
            "halving or growing by half or more in one year is treated as unfit, not a finding"
        )
        return "valorization_band", reason
    return "ok", None


# --------------------------------------------------------------------------- joint estimator (VERIFICATION 5.1)


def fit_joint(
    dt: np.ndarray,
    revenue: np.ndarray,
    cost: np.ndarray,
    *,
    alpha0: float,
    beta0: float,
    v0: float = 0.0,
    max_iter: int = 200,
    tol: float = 1e-10,
) -> tuple[float, float, float, bool, int]:
    """Levenberg-Marquardt fit of ``cost_t = alpha*(1+v)^dt_t + beta*revenue_t``.

    ``dt`` is ``year - t0`` (``t0`` = window_to, matching :meth:`FitResult.fixed_part_at`).
    Starts at ``(alpha0, v0, beta0)`` -- the OLS solution with ``v0=0`` by default, exactly the
    PDF's implicit assumption -- and takes damped Gauss-Newton steps, accepting a step only when
    it lowers the sum of squared residuals.

    Returns ``(alpha, v, beta, converged, iterations)``. ``converged`` is ``True`` only when a
    step's size falls below ``tol`` (relative to the parameter vector's norm) before
    ``max_iter`` is exhausted; the caller falls back to the OLS values when it is ``False``, as
    the PDF's estimator remains well-defined even when the joint one is not.
    """
    dt = np.asarray(dt, dtype=float)
    revenue = np.asarray(revenue, dtype=float)
    cost = np.asarray(cost, dtype=float)
    params = np.array([float(alpha0), float(v0), float(beta0)], dtype=float)

    def model_and_jacobian(p: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        a, v, b = p
        base = 1.0 + v
        pw = np.power(base, dt)
        # d/dv [a * (1+v)^dt] = a * dt * (1+v)^(dt-1); well-defined at dt=0 (value 0) even
        # when base is 0, since the dt==0 branch never evaluates (1+v)^(-1).
        pw_dv = np.where(dt == 0.0, 0.0, a * dt * np.power(base, dt - 1.0))
        yhat = a * pw + b * revenue
        jac = np.column_stack([pw, pw_dv, revenue])
        return yhat, jac

    def sse(p: np.ndarray) -> tuple[float, np.ndarray]:
        yhat, _ = model_and_jacobian(p)
        resid = yhat - cost
        return float(np.sum(resid * resid)), resid

    lam = 1e-3
    cur_cost, resid = sse(params)
    converged = False
    iterations = 0
    if not np.isfinite(cur_cost):
        return float(alpha0), float(v0), float(beta0), False, 0
    for iterations in range(1, max_iter + 1):
        _, jac = model_and_jacobian(params)
        jtj = jac.T @ jac
        jtr = jac.T @ resid
        try:
            delta = np.linalg.solve(jtj + lam * np.eye(3), -jtr)
        except np.linalg.LinAlgError:
            break
        candidate = params + delta
        if not np.all(np.isfinite(candidate)) or candidate[1] <= -1.0:
            lam *= 10.0
            if lam > 1e14:
                break
            continue
        new_cost, new_resid = sse(candidate)
        if np.isfinite(new_cost) and new_cost < cur_cost:
            step = float(np.linalg.norm(delta))
            scale = float(np.linalg.norm(params)) + 1e-12
            params, cur_cost, resid = candidate, new_cost, new_resid
            lam = max(lam * 0.3, 1e-12)
            if step < tol * scale:
                converged = True
                break
        else:
            lam *= 10.0
            if lam > 1e14:
                break
    alpha, v, beta = (float(x) for x in params)
    return alpha, v, beta, converged, int(iterations)


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
    method: str | None = None,
    degeneracy_share: float | None = None,
    joint_max_iter: int = 200,
    joint_tol: float = 1e-10,
    joint_beta_max: float | None = None,
    joint_valorization_min: float | None = None,
    joint_valorization_max: float | None = None,
) -> FitResult:
    """Fit ``Cost_t = alpha + beta * Revenue_t`` for one category over ``window``.

    ``subtract`` (indexed by year) is removed from the raw cost before the fit
    (used for OTH - DEPR); ``subtract_code`` labels it in the derivation.
    Registers derivation ``param:{code}``.

    ``method`` selects the estimator (``nvplan.config.REGRESSION_METHOD`` when omitted):

    * ``"ols"`` (default, the PDF's method, unchanged): constant intercept, ``v`` = mean YoY
      growth of the fixed part ``Cost_t - beta*Revenue_t``.
    * ``"joint"`` (VERIFICATION.md 5.1): :func:`fit_joint` fits alpha, v and beta together by
      non-linear least squares, starting from the OLS solution; falls back to OLS if it does
      not converge, or if it converges but fails the plausibility guard below.

    Either way the resulting valorization rate is guarded (VERIFICATION.md 5.2, see
    :func:`_valorization_guard`): when degenerate, ``FitResult.valorization_rate_raw`` is
    ``None`` and ``.valorization_status`` says why, while ``.valorization_rate`` -- what every
    downstream consumer (projection, the persisted parameter row) actually uses -- falls back
    to 0.0. ``degeneracy_share`` overrides ``nvplan.config.VALORIZATION_DEGENERACY_SHARE``.

    A *converged* joint fit is additionally checked for plausibility (VERIFICATION.md 5.4, see
    :func:`_plausibility_guard`): a negative or too-large beta, a negative alpha, an
    out-of-band v, or a non-finite value is rejected and falls back to the OLS solution, with
    the rejection recorded on ``FitResult.joint_status`` / ``.joint_rejection_reason`` and in
    the derivation rather than silently persisted. ``joint_beta_max``,
    ``joint_valorization_min`` and ``joint_valorization_max`` override
    ``nvplan.config.JOINT_BETA_MAX`` / ``.JOINT_VALORIZATION_MIN`` / ``.JOINT_VALORIZATION_MAX``.
    """
    if code not in wide.columns:
        raise KeyError(f"category {code!r} not in wide frame columns {list(wide.columns)}")
    if revenue_code not in wide.columns:
        raise KeyError(f"revenue code {revenue_code!r} not in wide frame")
    method = config.REGRESSION_METHOD if method is None else method
    if method not in REGRESSION_METHODS:
        raise ValueError(f"method={method!r} is not valid; expected one of {REGRESSION_METHODS}")
    share = config.VALORIZATION_DEGENERACY_SHARE if degeneracy_share is None else float(degeneracy_share)
    beta_max = config.JOINT_BETA_MAX if joint_beta_max is None else float(joint_beta_max)
    v_min = config.JOINT_VALORIZATION_MIN if joint_valorization_min is None else float(joint_valorization_min)
    v_max = config.JOINT_VALORIZATION_MAX if joint_valorization_max is None else float(joint_valorization_max)
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

    joint_converged: bool | None = None
    joint_iterations: int | None = None
    joint_status = "not_applicable"
    joint_rejection_reason: str | None = None
    joint_rejected_alpha: float | None = None
    joint_rejected_beta: float | None = None
    joint_rejected_v: float | None = None
    joint_fallback_alpha: float | None = None
    joint_fallback_beta: float | None = None
    v_raw: float
    if method == "joint":
        t0 = years[-1]
        dt = np.asarray(years, dtype=float) - float(t0)
        alpha_j, v_j, beta_j, joint_converged, joint_iterations = fit_joint(
            dt, rev, cost, alpha0=alpha, beta0=beta, max_iter=joint_max_iter, tol=joint_tol
        )
        plausible_status, plausible_reason = (
            _plausibility_guard(alpha_j, beta_j, v_j, beta_max=beta_max, v_min=v_min, v_max=v_max)
            if joint_converged
            else ("not_converged", None)
        )
        if joint_converged and plausible_status == "ok":
            joint_status = "accepted"
            alpha, beta, v_raw = alpha_j, beta_j, v_j
            model = alpha * np.power(1.0 + v_raw, dt) + beta * rev
            resid = cost - model
            fitted = model
            ss_res = float(np.sum(resid**2))
            ss_tot = float(np.sum((cost - cost.mean()) ** 2))
            r2 = 1.0 if ss_tot == 0.0 else 1.0 - ss_res / ss_tot
            if ss_tot == 0.0:
                r2 = 1.0 if ss_res == 0.0 else 0.0
        elif joint_converged:
            # converged but implausible (VERIFICATION.md 5.4): fall back entirely to the OLS
            # solution already computed above, and record the rejection honestly rather than
            # persisting the converged-but-absurd fit or hiding that a joint fit was attempted.
            joint_status = "rejected"
            joint_rejection_reason = plausible_reason
            joint_rejected_alpha, joint_rejected_beta, joint_rejected_v = alpha_j, beta_j, v_j
            v_raw, _ = _mean_yoy_growth(cost - beta * rev)
            joint_fallback_alpha, joint_fallback_beta = alpha, beta
        else:
            # not converged: fall back entirely to the OLS solution already computed above
            joint_status = "not_converged"
            v_raw, _ = _mean_yoy_growth(cost - beta * rev)
    else:
        v_raw, _ = _mean_yoy_growth(cost - beta * rev)

    fixed = cost - beta * rev  # == alpha + resid, with whichever beta was actually used
    _, growth = _mean_yoy_growth(fixed)  # observed YoY growth of the fixed part, for the trace
    mean_cost = float(np.mean(cost))
    valorization_status, fallback_reason = _valorization_guard(alpha, fixed, mean_cost, share)
    if valorization_status == "ok":
        valorization_rate_raw: float | None = v_raw
        valorization_rate = v_raw
    else:
        valorization_rate_raw = None
        valorization_rate = 0.0  # VERIFICATION.md 5.2's fallback: valorize at 0 when undefined

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
    if method == "joint":
        formula = (
            f"Cost_t = alpha*(1+v)^(t-t0) + beta*Revenue_t (joint non-linear least squares, "
            f"window {years[0]}-{years[-1]}, started from OLS)"
        )
        if joint_status == "not_converged":
            formula += "; did not converge, fell back to OLS: Cost_t = alpha + beta*Revenue_t + eps"
        elif joint_status == "rejected":
            formula += (
                f"; converged but rejected by the plausibility guard ({joint_rejection_reason}), "
                "fell back to OLS: Cost_t = alpha + beta*Revenue_t + eps"
            )
    else:
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
            "v": valorization_rate,
            "n": len(years),
            "window_from": years[0],
            "window_to": years[-1],
            "calc_version": CALC_VERSION,
            "method": method,
            "valorization_status": valorization_status,
            "valorization_rate_raw": valorization_rate_raw,
            "valorization_fallback_reason": fallback_reason,
            "valorization_degeneracy_share": share,
            "joint_converged": joint_converged,
            "joint_iterations": joint_iterations,
            "joint_status": joint_status,
            "joint_rejection_reason": joint_rejection_reason,
            "joint_rejected_alpha": joint_rejected_alpha,
            "joint_rejected_beta": joint_rejected_beta,
            "joint_rejected_v": joint_rejected_v,
            "joint_fallback_alpha": joint_fallback_alpha,
            "joint_fallback_beta": joint_fallback_beta,
            "joint_beta_max": beta_max,
            "joint_valorization_min": v_min,
            "joint_valorization_max": v_max,
        },
        parents=(),
    )
    return FitResult(
        category_code=code,
        alpha=alpha,
        beta=beta,
        r_squared=r2,
        valorization_rate=valorization_rate,
        window_from=years[0],
        window_to=years[-1],
        n=len(years),
        points=points,
        fixed_part_series=fixed_series,
        derivation_key=key,
        component_code=subtract_code if subtract is not None else None,
        method=method,
        valorization_status=valorization_status,
        valorization_rate_raw=valorization_rate_raw,
        valorization_fallback_reason=fallback_reason,
        joint_converged=joint_converged,
        joint_iterations=joint_iterations,
        joint_status=joint_status,
        joint_rejection_reason=joint_rejection_reason,
        joint_rejected_alpha=joint_rejected_alpha,
        joint_rejected_beta=joint_rejected_beta,
        joint_rejected_v=joint_rejected_v,
        joint_fallback_alpha=joint_fallback_alpha,
        joint_fallback_beta=joint_fallback_beta,
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
    method: str | None = None,
    degeneracy_share: float | None = None,
) -> dict[str, FitResult]:
    """Fit every cost category. ``component_of`` (OTH) is regressed net of ``depreciation``.

    If ``depreciation`` is None the ``DEPR`` column of ``wide`` is used when present.
    ``method`` and ``degeneracy_share`` are forwarded to :func:`fit_category` for every
    category (``nvplan.config`` defaults when omitted; see VERIFICATION.md 5.1 / 5.2).
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
                method=method, degeneracy_share=degeneracy_share,
            )
        else:
            out[code] = fit_category(
                wide, code, window, ledger=ledger, revenue_code=revenue_code,
                method=method, degeneracy_share=degeneracy_share,
            )
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
