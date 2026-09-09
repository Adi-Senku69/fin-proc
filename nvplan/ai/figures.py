"""The deterministic plan-vs-actual table and the numeric cross-check of an AI explanation.

PDF rule: "deviation calculated deterministically, explained by AI". One code path produces
the figures - :func:`plan_vs_actual` - and two consumers read it:

* the ``get_plan_vs_actual`` tool (``nvplan.ai.tools``), i.e. what the model is shown;
* :func:`check_explanation`, which ``run_deviation_explanation`` applies to the structured
  ``DeviationExplanation`` after the run. Every ``Contribution`` must name a category of the
  table, all categories of the table must be covered exactly once, ``plan``/``actual``/
  ``deviation`` must equal the deterministic value within ``ABS_TOL`` (0.05 k EUR, i.e. equal
  when rounded to one decimal), and the contribution's ``explanation`` text must cite its
  deviation figure to one decimal. Any violation raises :class:`ExplanationRejected` with the
  offending fields (model value vs deterministic value) and nothing is persisted.

The citation check is tolerant of formatting only: ``-245.7``, ``-1,245.7``, ``-1 245.7``,
``+245.7`` / ``245.7`` (positive), a unicode minus, are all accepted; a different number,
a different rounding or no number at all is not.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from nvplan.db.models import Actual, Category, Parameter, PlanValue, Scenario, ScenarioKind

ABS_TOL = 0.05  # k EUR: model figure and deterministic figure must agree to one decimal
CHECKED_FIELDS: tuple[str, ...] = ("plan", "actual", "deviation")


class ExplanationRejected(ValueError):
    """The structured deviation explanation contradicts the deterministic table; nothing was written."""


# --------------------------------------------------------------------------- lookups


def _categories(session: Session) -> dict[int, Category]:
    return {c.id: c for c in session.scalars(select(Category)).all()}


def _scenario_by_kind(session: Session, kind: str) -> Scenario | None:
    try:
        sk = ScenarioKind(kind.strip().lower())
    except ValueError:
        return None
    return session.scalars(select(Scenario).where(Scenario.kind == sk).order_by(Scenario.id.desc())).first()


def _latest_parameters(session: Session) -> dict[int, Parameter]:
    """Latest parameter row per category (highest id wins; rows are immutable/versioned)."""
    latest: dict[int, Parameter] = {}
    for p in session.scalars(select(Parameter).order_by(Parameter.id)).all():
        latest[p.category_id] = p
    return latest


# --------------------------------------------------------------------------- the table


def plan_vs_actual(session: Session, scenario_kind: str, year: int) -> dict[str, Any]:
    """Pure arithmetic: plan_value vs actual per (non-component) category for one scenario/year.

    deviation = actual - plan; deviation_pct relative to plan. Parameters (alpha, beta,
    R^2) are attached so the caller can split cost deviations into beta * revenue
    deviation and residual.
    """
    scenario = _scenario_by_kind(session, scenario_kind)
    if scenario is None:
        return {"error": f"no scenario of kind {scenario_kind!r}"}
    cats = _categories(session)
    params = _latest_parameters(session)
    plans = {
        pv.category_id: pv
        for pv in session.scalars(
            select(PlanValue).where(PlanValue.scenario_id == scenario.id, PlanValue.year == year)
        ).all()
    }
    actuals = {a.category_id: a for a in session.scalars(select(Actual).where(Actual.year == year)).all()}
    rows: list[dict[str, Any]] = []
    for cid, cat in sorted(cats.items(), key=lambda kv: kv[0]):
        if cat.is_component or cid not in plans or cid not in actuals:
            continue
        plan = float(plans[cid].value)
        actual = float(actuals[cid].value)
        dev = actual - plan
        p = params.get(cid)
        rows.append(
            {
                "category_code": cat.code,
                "name": cat.name,
                "kind": cat.kind.value,
                "plan": plan,
                "actual": actual,
                "deviation": dev,
                "deviation_pct": (dev / plan * 100.0) if plan else None,
                "plan_path": plans[cid].path.value,
                "alpha": p.alpha if p else None,
                "beta": p.beta if p else None,
                "r_squared": p.r_squared if p else None,
                "valorization_rate": p.valorization_rate if p else None,
            }
        )
    rev = next((r for r in rows if r["kind"] == "revenue"), None)
    rev_dev = rev["deviation"] if rev else None
    for r in rows:
        if r["kind"] == "cost" and r["beta"] is not None and rev_dev is not None:
            r["revenue_driven_part"] = r["beta"] * rev_dev
            r["residual"] = r["deviation"] - r["revenue_driven_part"]
    costs = [r for r in rows if r["kind"] == "cost"]
    totals = {
        "revenue_plan": rev["plan"] if rev else None,
        "revenue_actual": rev["actual"] if rev else None,
        "cost_plan": sum(r["plan"] for r in costs),
        "cost_actual": sum(r["actual"] for r in costs),
    }
    if rev:
        totals["result_plan"] = rev["plan"] - totals["cost_plan"]
        totals["result_actual"] = rev["actual"] - totals["cost_actual"]
    return {
        "scenario": scenario.kind.value,
        "scenario_label": scenario.label,
        "year": year,
        "unit": "k EUR",
        "deviation_sign": "actual - plan",
        "rows": rows,
        "totals": totals,
    }


# --------------------------------------------------------------------------- the cross-check


def figure_pattern(value: float) -> re.Pattern[str]:
    """Regex matching ``value`` written to one decimal in prose, tolerant of formatting.

    Thousands separators: none, comma or space. Sign: a negative value needs a leading
    ``-``/unicode minus; a positive value may carry ``+`` or nothing; zero accepts any.
    Guarded so ``5.0`` does not match inside ``15.0`` or ``5.03``.
    """
    rounded = round(float(value), 1)
    digits = f"{abs(rounded):.1f}"  # e.g. "1245.7"
    whole, frac = digits.split(".")
    groups = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    groups.insert(0, whole)
    sep = "[ ,\u00a0\u202f]?"  # none, space, comma, (narrow) no-break space
    body = sep.join(re.escape(g) for g in groups) + r"\." + frac
    minus = "[-\u2212\u2013]"  # hyphen-minus, unicode minus, en dash
    if rounded < 0:
        sign = rf"{minus}\s?"
    elif rounded > 0:
        sign = r"(?:\+\s?)?"
    else:
        sign = rf"(?:(?:\+|{minus})\s?)?"
    # not preceded by a digit/decimal point (5.0 inside 15.0 or 5.03), a minus (a positive figure
    # never matches "-245.7") or "<digit><sep>" (420.0 inside "6 420.0"); not followed by a digit.
    return re.compile(rf"(?<![\d.,\-\u2212\u2013])(?<!\d[ ,\u00a0\u202f]){sign}{body}(?!\d)")


def mentions_figure(text: str, value: float) -> bool:
    """True when ``text`` cites ``value`` to one decimal (see :func:`figure_pattern`)."""
    return bool(text) and figure_pattern(value).search(text) is not None


def _fmt(v: Any) -> str:
    return f"{v:.1f}" if isinstance(v, (int, float)) else repr(v)


def check_explanation(explanation: Any, table: dict[str, Any], *, abs_tol: float = ABS_TOL) -> None:
    """Verify a ``DeviationExplanation`` against the deterministic ``plan_vs_actual`` table.

    Raises :class:`ExplanationRejected` listing every offending field; returns None when
    the explanation is consistent. ``explanation`` is the pydantic object (or anything with
    ``.contributions`` whose items have ``category_code``, ``plan``, ``actual``, ``deviation``,
    ``explanation``).
    """
    if "error" in table:
        raise ExplanationRejected(f"no deterministic table to check against: {table['error']}")
    expected = {r["category_code"]: r for r in table.get("rows", [])}
    problems: list[str] = []
    seen: dict[str, int] = {}
    for c in explanation.contributions:
        code = str(c.category_code).strip().upper()
        seen[code] = seen.get(code, 0) + 1
        det = expected.get(code)
        if det is None:
            problems.append(f"{code}: not in the deterministic table (categories: {sorted(expected)})")
            continue
        for field in CHECKED_FIELDS:
            model_v = getattr(c, field)
            det_v = det[field]
            if not isinstance(model_v, (int, float)) or abs(float(model_v) - float(det_v)) > abs_tol + 1e-9:
                problems.append(f"{code}.{field}: model {_fmt(model_v)} vs deterministic {det_v:.1f}")
        if not mentions_figure(c.explanation or "", det["deviation"]):
            problems.append(
                f"{code}.explanation: does not cite its deviation {det['deviation']:+.1f} "
                f"(text: {(c.explanation or '')[:120]!r})"
            )
    for code, n in seen.items():
        if n > 1:
            problems.append(f"{code}: listed {n} times")
    missing = sorted(set(expected) - set(seen))
    if missing:
        problems.append(f"missing contributions for {missing}")
    if problems:
        raise ExplanationRejected(
            f"deviation explanation rejected ({len(problems)} mismatch(es) against the deterministic "
            f"plan-vs-actual table, scenario {table.get('scenario')!r} year {table.get('year')}): "
            + "; ".join(problems)
        )
