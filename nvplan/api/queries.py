"""Read-only helpers over a Session, shared by the API routes and the demo.

Nothing here writes plan numbers. The two writers are ``seed_external_notes``
(the illustrative notes, once) and nothing else; every planning write goes
through ``services.planning`` / ``services.gate``.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nvplan import config
from nvplan.core import DerivationLedger, to_wide
from nvplan.core import kpi
from nvplan.core.backtest import BacktestCase, BacktestResult, run_backtest
from nvplan.core.deviation import compute_deviation
from nvplan.db.models import (
    Actual,
    AiRecord,
    AiStatus,
    Category,
    Derivation,
    ExternalNote,
    NoteSource,
    Parameter,
    PlanValue,
    Scenario,
    ScenarioKind,
    Statement,
    StatementLine,
    Touchpoint,
)
from nvplan.db.session import category_map
from nvplan.ingest.actuals import actuals_frame
from nvplan.services.planning import KEY_FIELD

# The brain / bridge index (PLATFORM.md §6, §7.1; UI.md Part 1). ``provenance`` is the
# shared provenance core - nvplan importing it is the same, already-established choice
# ``nvplan.services.trace`` makes (see that module's docstring): it is not ``brainkit``,
# which stays off-limits to nvplan proper. ``bridge.effects`` is imported too, for the one
# read-only reuse (``decided_effects``) that the ``/brain/effects`` route needs verbatim
# rather than re-deriving the same "decided decision with an effect" query a second time;
# ``nvplan/api`` is the platform's demo/composition layer, the intended exception to the
# "nvplan never imports brainkit" rule (PLATFORM.md §7.1), not the core engine.
from provenance import Claim, ClaimKind, ClaimLink, DecisionStatus, Evidence, HypothesisStatus

from bridge.effects import decided_effects as _decided_effects

GRID_CODES: list[str] = [*config.CATEGORY_CODES, *config.COMPONENT_CODES]  # REV, MAT, EXT, PERS, OTH, DEPR
COST_CODES: list[str] = [c for c in config.CATEGORY_CODES if c != "REV"]


# --------------------------------------------------------------------------- generic


def illustrative_flag(session: Session) -> bool:
    """True if any actual is labelled ILLUSTRATIVE. Surfaced on every grid; never hidden."""
    n = session.scalar(
        select(func.count(Actual.id)).where(Actual.source_label.contains(config.ILLUSTRATIVE_LABEL))
    )
    return bool(n)


def scenario_kind(kind: str | ScenarioKind) -> ScenarioKind:
    if isinstance(kind, ScenarioKind):
        return kind
    try:
        return ScenarioKind(str(kind).strip().lower())
    except ValueError as e:
        raise LookupError(f"unknown scenario kind {kind!r}; expected best / base / worst") from e


def latest_scenario(session: Session, kind: str | ScenarioKind, scenario_id: int | None = None) -> Scenario:
    """The scenario with ``scenario_id`` (must be of ``kind``) or the latest one of that kind."""
    k = scenario_kind(kind)
    if scenario_id is not None:
        scen = session.get(Scenario, int(scenario_id))
        if scen is None:
            raise LookupError(f"scenario {scenario_id} not found")
        if scen.kind is not k:
            raise LookupError(f"scenario {scenario_id} is {scen.kind.value}, not {k.value}")
        return scen
    scen = session.scalars(select(Scenario).where(Scenario.kind == k).order_by(Scenario.id.desc())).first()
    if scen is None:
        raise LookupError(f"no scenario of kind {k.value!r} - run the plan first")
    return scen


def _iso(dt) -> str | None:
    return dt.isoformat() if dt is not None else None


def scenario_dict(s: Scenario) -> dict[str, Any]:
    return {"id": s.id, "kind": s.kind.value, "label": s.label, "created_by": s.created_by,
            "created_at": _iso(s.created_at)}


def list_scenarios(session: Session) -> list[dict[str, Any]]:
    return [scenario_dict(s) for s in session.scalars(select(Scenario).order_by(Scenario.id)).all()]


def table_counts(session: Session) -> dict[str, int]:
    from nvplan.db.models import ALL_TABLES

    return {t.__tablename__: int(session.scalar(select(func.count()).select_from(t))) for t in ALL_TABLES}


# --------------------------------------------------------------------------- notes


def seed_external_notes(session: Session, path: str | Path | None = None) -> int:
    """Insert the illustrative manual notes (external_notes.csv) once; 0 if manual notes exist."""
    path = Path(path) if path is not None else config.DATA_DIR / "external_notes.csv"
    if not path.exists():
        return 0
    existing = session.scalar(select(func.count(ExternalNote.id)).where(ExternalNote.source == NoteSource.manual))
    if existing:
        return 0
    cats = category_map(session)
    n = 0
    for row in pd.read_csv(path).itertuples(index=False):
        code = str(row.category_code).strip().upper() if isinstance(row.category_code, str) else None
        session.add(
            ExternalNote(
                category_id=cats[code].id if code in cats else None,
                year=int(row.year) if not pd.isna(row.year) else None,
                text=str(row.text),
                author=str(row.author),
                source=NoteSource(str(row.source)),
            )
        )
        n += 1
    session.commit()
    return n


def note_ids(session: Session, *, category_code: str | None = None, year: int | None = None) -> list[int]:
    stmt = select(ExternalNote.id).order_by(ExternalNote.id)
    if category_code is not None:
        cat = session.scalar(select(Category).where(Category.code == category_code))
        stmt = stmt.where(ExternalNote.category_id == (cat.id if cat else -1))
    if year is not None:
        stmt = stmt.where(ExternalNote.year == int(year))
    return [int(i) for i in session.scalars(stmt).all()]


# --------------------------------------------------------------------------- plan grid


def _find_ancestor_key(session: Session, derivation_id: int, key: str, max_depth: int = 8) -> Derivation | None:
    """Breadth-first search up ``parent_ids_json`` for the derivation whose ledger key is ``key``."""
    frontier = [int(derivation_id)]
    seen: set[int] = set()
    for _ in range(max_depth + 1):
        nxt: list[int] = []
        for did in frontier:
            if did in seen:
                continue
            seen.add(did)
            d = session.get(Derivation, did)
            if d is None:
                continue
            if (d.inputs_json or {}).get(KEY_FIELD) == key:
                return d
            nxt.extend(int(p) for p in (d.parent_ids_json or []))
        if not nxt:
            return None
        frontier = nxt
    return None


def scenario_parameters(session: Session, scen: Scenario) -> list[dict[str, Any]]:
    """The parameter rows this scenario's numbers were computed with (via the lineage, not timestamps)."""
    cats = category_map(session)
    values = session.scalars(select(PlanValue).where(PlanValue.scenario_id == scen.id).order_by(PlanValue.year)).all()
    first_by_code: dict[str, PlanValue] = {}
    for pv in values:
        code = next(c for c in cats.values() if c.id == pv.category_id).code
        first_by_code.setdefault(code, pv)
    out: list[dict[str, Any]] = []
    rev = first_by_code.get("REV")
    if rev is not None:
        g = _find_ancestor_key(session, rev.derivation_id, "param:REV")
        params = (g.parameters_json or {}) if g else {}
        out.append({"category_code": "REV", "growth_rate": params.get("g"), "window_from": params.get("window_from"),
                    "window_to": params.get("window_to"), "calc_version": params.get("calc_version")})
    for code in COST_CODES:
        pv = first_by_code.get(code)
        row: dict[str, Any] = {"category_code": code}
        if pv is not None:
            d = _find_ancestor_key(session, pv.derivation_id, f"param:{code}")
            prm = session.scalars(select(Parameter).where(Parameter.derivation_id == d.id)).first() if d else None
            if prm is not None:
                row.update(parameter_id=prm.id, alpha=prm.alpha, beta=prm.beta, r_squared=prm.r_squared,
                           valorization_rate=prm.valorization_rate, window_from=prm.window_from,
                           window_to=prm.window_to, calc_version=prm.calc_version)
        out.append(row)
    return out


def plan_grid(session: Session, kind: str | ScenarioKind = "base", scenario_id: int | None = None) -> dict[str, Any]:
    """Rows = categories (REV, MAT, EXT, PERS, OTH, DEPR), columns = plan years, cell = value + provenance ids."""
    scen = latest_scenario(session, kind, scenario_id)
    cats = category_map(session)
    by_id = {c.id: c for c in cats.values()}
    values = session.scalars(
        select(PlanValue).where(PlanValue.scenario_id == scen.id).order_by(PlanValue.category_id, PlanValue.year)
    ).all()
    years = sorted({pv.year for pv in values})
    cells: dict[str, dict[int, dict[str, Any]]] = {code: {} for code in GRID_CODES}
    for pv in values:
        code = by_id[pv.category_id].code
        cells.setdefault(code, {})[pv.year] = {
            "value": pv.value, "path": pv.path.value, "plan_value_id": pv.id, "ai_record_id": pv.ai_record_id,
        }
    rows = []
    for code in [c for c in GRID_CODES if c in cats] + [c for c in cells if c not in GRID_CODES]:
        cat = cats[code]
        rows.append({"category_code": code, "name": cat.name, "kind": cat.kind.value,
                     "is_component": cat.is_component, "cells": cells.get(code, {})})
    return {
        "scenario_id": scen.id, "scenario_kind": scen.kind.value, "scenario_label": scen.label,
        "created_by": scen.created_by, "created_at": _iso(scen.created_at),
        "illustrative": illustrative_flag(session), "unit": "k EUR", "years": years, "rows": rows,
        "parameters": scenario_parameters(session, scen),
    }


def default_revenue(session: Session, kind: str | ScenarioKind, year: int, scenario_id: int | None = None) -> float:
    """The valorized default revenue of ``year`` in the latest plan of ``kind``.

    If that year's REV is already ``ai_proposed`` the default it replaced is read from
    the derivation (``inputs.default_value``), so a second proposal is judged against
    the same default as the first."""
    from nvplan.services.trace import find_plan_value

    pv = find_plan_value(session, scenario_kind=kind, category_code="REV", year=int(year), scenario_id=scenario_id)
    if pv.path.value == "valorized":
        return float(pv.value)
    d = session.get(Derivation, pv.derivation_id)
    inputs = d.inputs_json or {}
    if "default_value" in inputs:
        return float(inputs["default_value"])
    dflt = _find_ancestor_key(session, pv.derivation_id, f"plan:default:REV:{int(year)}")
    if dflt is not None and "value" in (dflt.parameters_json or {}):
        return float(dflt.parameters_json["value"])
    raise LookupError(f"cannot determine the valorized default revenue for {year} (path {pv.path.value})")


# --------------------------------------------------------------------------- statements


def statement_grid(session: Session, kind: str | ScenarioKind, statement: str, scenario_id: int | None = None) -> dict[str, Any]:
    scen = latest_scenario(session, kind, scenario_id)
    try:
        stmt_enum = Statement(str(statement).lower())
    except ValueError as e:
        raise LookupError(f"unknown statement {statement!r}; expected pl / bs / cf") from e
    lines = session.scalars(
        select(StatementLine).where(StatementLine.scenario_id == scen.id, StatementLine.statement == stmt_enum)
        .order_by(StatementLine.id)
    ).all()
    years = sorted({sl.year for sl in lines})
    order: list[str] = []
    cells: dict[str, dict[int, dict[str, Any]]] = {}
    for sl in lines:
        if sl.line_code not in cells:
            order.append(sl.line_code)
            cells[sl.line_code] = {}
        cells[sl.line_code][sl.year] = {"value": sl.value, "statement_line_id": sl.id, "mapping_ref": sl.mapping_ref}
    return {
        "scenario_id": scen.id, "scenario_kind": scen.kind.value, "scenario_label": scen.label,
        "statement": stmt_enum.value, "illustrative": illustrative_flag(session), "unit": "k EUR",
        "years": years, "rows": [{"line_code": lc, "cells": cells[lc]} for lc in order],
        "consistency": statement_consistency(session, scen) if stmt_enum is Statement.bs else [],
    }


def statement_consistency(session: Session, scen: Scenario, tol: float = 1e-6) -> list[str]:
    """Re-check the persisted BS / CF of a scenario: balance every year and cash tie. Empty = clean."""
    rows = session.scalars(select(StatementLine).where(StatementLine.scenario_id == scen.id)).all()
    bs: dict[int, dict[str, float]] = {}
    cf: dict[int, dict[str, float]] = {}
    for sl in rows:
        target = bs if sl.statement is Statement.bs else cf if sl.statement is Statement.cf else None
        if target is not None:
            target.setdefault(sl.year, {})[sl.line_code] = sl.value
    problems: list[str] = []
    for y in sorted(bs):
        r = bs[y]
        if abs(r["total_assets"] - r["total_liabilities_equity"]) > tol:
            problems.append(f"{y}: BS does not balance ({r['total_assets']} vs {r['total_liabilities_equity']})")
        if (y - 1) in bs and y in cf:
            if abs((r["cash"] - bs[y - 1]["cash"]) - cf[y]["net_cash_flow"]) > tol:
                problems.append(f"{y}: delta cash != net_cash_flow")
            if abs(bs[y]["equity"] - bs[y - 1]["equity"] - cf[y]["net_income"]) > tol:
                problems.append(f"{y}: delta equity != net_income")
    return problems


# --------------------------------------------------------------------------- kpis (C2)

#: Which KPI input names are read off a figure that ultimately rolls forward from the opening
#: balance sheet (bs_mapping.yaml's ``opening_balance_sheet`` -- itself an assumption, not a
#: measured position; see nvplan.core.statements). P&L-only inputs (revenue, total_costs, ebit,
#: personnel, external) are not affected by the opening position and are excluded.
BALANCE_SHEET_KPI_INPUTS: frozenset[str] = frozenset(
    {"cash", "receivables", "payables", "equity", "total_assets", "operating_cf", "investment"}
)


def _kpi_inputs_by_year(session: Session, scen: Scenario) -> dict[int, dict[str, float]]:
    """Assemble the named figures ``nvplan.core.kpi.CATALOGUE`` draws on, per plan year, from the
    persisted PL / BS / CF lines of one scenario.

    Only years with a P&L row are returned: the balance sheet also carries one extra row for the
    opening year (``first plan year - 1``, ``nvplan.core.statements.build_bs``'s
    ``mapping_ref="opening_balance_sheet"``), which is not a plan year the KPI report claims to
    cover -- ``test_statements.py`` already excludes it from the P&L's own ``years``, and this
    does the same rather than silently reporting one extra (partial) year of KPIs nobody asked for.

    Kept in one place, reading only ``nvplan.core.kpi.STATEMENT_INPUTS``, so a KPI definition can
    only reference a figure this function actually supplies -- never a name that happens to look
    plausible. ``investment`` (``nvplan.core.kpi.DERIVED_INPUTS``) is the one figure the statement
    chain does not carry directly: the CF frame only has the sign-flipped ``investing_cf``
    (``investing_cf = -capex``), so it is derived here, once, rather than every KPI needing to
    know the sign convention.
    """
    lines = session.scalars(select(StatementLine).where(StatementLine.scenario_id == scen.id)).all()
    by_year: dict[int, dict[str, float]] = {}
    pl_years: set[int] = set()
    for sl in lines:
        by_year.setdefault(sl.year, {})[sl.line_code] = sl.value
        if sl.statement is Statement.pl:
            pl_years.add(sl.year)
    inputs: dict[int, dict[str, float]] = {}
    for year in pl_years:
        values = by_year[year]
        row = {name: values[name] for name in kpi.STATEMENT_INPUTS if name in values}
        if "investing_cf" in values:
            row["investment"] = -values["investing_cf"]
        inputs[year] = row
    return inputs


def kpi_grid(session: Session, kind: str | ScenarioKind = "base", scenario_id: int | None = None) -> dict[str, Any]:
    """The Kennzahlen: ratios derived from the persisted statements of one scenario.

    A category is a sum, a KPI is a ratio, and every value carries the inputs it was computed
    from and the threshold applied (``nvplan.core.kpi``), because a traffic light whose standard
    is invisible is a decoration. A definition whose inputs are not (yet) all persisted for a
    year is skipped for that year rather than raising (``nvplan.core.kpi.evaluate_all``'s own
    rule); a definition with no computable year at all is left out of the response entirely.
    """
    scen = latest_scenario(session, kind, scenario_id)
    inputs_by_year = _kpi_inputs_by_year(session, scen)
    years = sorted(inputs_by_year)
    illustrative = illustrative_flag(session)

    kpis: list[dict[str, Any]] = []
    for definition in kpi.CATALOGUE:
        values: list[dict[str, Any]] = []
        for year in years:
            try:
                computed = kpi.evaluate(definition, inputs_by_year[year], year)
            except kpi.MissingInputError:
                continue
            values.append({"year": year, "value": computed.value, "status": computed.status.value})
        if not values:
            continue
        used_inputs = sorted({*definition.numerator, *definition.denominator})
        kpis.append({
            "code": definition.code, "name": definition.name, "quadrant": definition.quadrant.value,
            "unit": definition.unit, "direction": definition.direction.value,
            "description": definition.description, "formula": definition.formula(), "inputs": used_inputs,
            "rests_on_opening_position": illustrative and bool(BALANCE_SHEET_KPI_INPUTS & set(used_inputs)),
            "thresholds": (
                None if definition.thresholds is None
                else {"good": definition.thresholds.good, "warn": definition.thresholds.warn,
                      "source": definition.thresholds.source}
            ),
            "values": values,
        })

    return {
        "scenario_id": scen.id, "scenario_kind": scen.kind.value, "scenario_label": scen.label,
        "illustrative": illustrative, "years": years, "kpis": kpis,
    }


# --------------------------------------------------------------------------- ai records


def ai_record_dict(session: Session, rec: AiRecord) -> dict[str, Any]:
    code = None
    if rec.category_id is not None:
        cat = session.get(Category, rec.category_id)
        code = cat.code if cat else None
    notes = [int(i) for i in session.scalars(select(ExternalNote.id).where(ExternalNote.ai_record_id == rec.id)).all()]
    return {
        "id": rec.id, "touchpoint": rec.touchpoint.value, "status": rec.status.value,
        "model_version": rec.model_version, "proposed_value": rec.proposed_value, "scenario_id": rec.scenario_id,
        "category_code": code, "year": rec.year, "rationale": rec.rationale, "prompt_text": rec.prompt_text,
        "response_text": rec.response_text, "confirmed_by": rec.confirmed_by,
        "confirmed_at": _iso(rec.confirmed_at), "created_at": _iso(rec.created_at), "note_ids": notes,
    }


def list_ai_records(session: Session, touchpoint: str | None = None, status: str | None = None) -> list[AiRecord]:
    stmt = select(AiRecord).order_by(AiRecord.id)
    if touchpoint:
        try:
            stmt = stmt.where(AiRecord.touchpoint == Touchpoint(touchpoint))
        except ValueError as e:
            raise LookupError(f"unknown touchpoint {touchpoint!r}") from e
    if status:
        try:
            stmt = stmt.where(AiRecord.status == AiStatus(status))
        except ValueError as e:
            raise LookupError(f"unknown status {status!r}") from e
    return list(session.scalars(stmt).all())


# --------------------------------------------------------------------------- backtest


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rec in df.to_dict(orient="records"):
        clean: dict[str, Any] = {}
        for k, v in rec.items():
            if isinstance(v, float) and not math.isfinite(v):
                clean[k] = None
            elif hasattr(v, "item"):  # numpy scalar
                clean[k] = v.item()
            else:
                clean[k] = v
        out.append(clean)
    return out


def wide_actuals(session: Session) -> pd.DataFrame:
    actuals = actuals_frame(session, include_components=True)
    if actuals.empty:
        raise LookupError("no actuals in the database - ingest first")
    return to_wide(actuals)


def backtest(session: Session, window_len: int = 5, horizons: tuple[int, ...] = (1, 2, 3)) -> BacktestResult:
    wide = wide_actuals(session)
    if "DEPR" not in wide.columns:
        raise LookupError("actuals have no DEPR series")
    return run_backtest(wide, ledger=DerivationLedger(), window_len=window_len, horizons=horizons,
                        depreciation=wide["DEPR"])


def backtest_report(session: Session, window_len: int = 5) -> dict[str, Any]:
    res = backtest(session, window_len=window_len)
    return {
        "n_cases": len(res.cases),
        "thresholds": {"mape_within": res.threshold_low, "mape_marginal": res.threshold_high},
        "summary": _records(res.summary),
        "summary_default_path": _records(res.summary_default_path),
        "fits": _records(res.fits),
        "skipped": _records(res.skipped),
        "markdown": res.to_markdown(),
        "illustrative": illustrative_flag(session),
    }


# --------------------------------------------------------------------------- deviation


def deviation(session: Session, kind: str | ScenarioKind = "base", year: int | None = None,
              scenario_id: int | None = None) -> dict[str, Any]:
    """``compute_deviation`` between the latest plan of ``kind`` and the actuals, for the years that have both."""
    scen = latest_scenario(session, kind, scenario_id)
    cats = category_map(session)
    by_id = {c.id: c.code for c in cats.values()}
    values = session.scalars(select(PlanValue).where(PlanValue.scenario_id == scen.id)).all()
    plan_long = pd.DataFrame(
        [{"scenario": scen.kind.value, "category_code": by_id[pv.category_id], "year": pv.year, "value": pv.value,
          "derivation_key": f"plan:{scen.kind.value}:{by_id[pv.category_id]}:{pv.year}"} for pv in values],
        columns=["scenario", "category_code", "year", "value", "derivation_key"],
    )
    actual_long = actuals_frame(session, include_components=True)
    ledger = DerivationLedger()
    for key in plan_long["derivation_key"]:
        ledger.add(key, "persisted plan value", inputs={})
    dev = compute_deviation(plan_long, actual_long[["category_code", "year", "value"]], scenario=scen.kind.value,
                            ledger=ledger, years=[int(year)] if year is not None else None)
    years = sorted({int(y) for y in dev["year"]}) if not dev.empty else []
    note = ("no year has both a plan value and an actual in this scenario"
            if dev.empty else "deviation = actual - plan; deviation_pct = deviation / plan")
    return {"scenario_id": scen.id, "scenario_kind": scen.kind.value, "scenario_label": scen.label,
            "years": years, "rows": _records(dev), "note": note}


def backtest_year_deviation(session: Session, year: int, window_len: int = 5) -> dict[str, Any]:
    """Plan-vs-actual for a KNOWN year without persisting anything.

    Fits on the ``window_len`` years ending ``year - 1`` and projects ``year`` (horizon 1) with
    :func:`nvplan.core.backtest.run_backtest`; REV is compared on the valorized default
    path, the costs on both bases (given the actual revenue, and along the default path)."""
    wide = wide_actuals(session)
    lo, hi = int(year) - window_len, int(year) - 1
    if lo not in wide.index or int(year) not in wide.index:
        raise LookupError(f"actuals do not cover {lo}-{year}")
    res = run_backtest(wide, cases=[BacktestCase((lo, hi), int(year), 1)], ledger=DerivationLedger(),
                       depreciation=wide["DEPR"])
    err = res.errors.drop(columns=["case", "train_from", "train_to", "horizon"])
    err = err.rename(columns={"error": "deviation_plan_minus_actual"})
    return {"year": int(year), "train_window": [lo, hi], "rows": _records(err), "fits": _records(res.fits),
            "note": ("not persisted; REV on basis default_revenue is the valorized default path, "
                     "costs on basis actual_revenue are the cascade given the actual revenue")}


# --------------------------------------------------------------------------- brain / bridge


_VALID_CLAIM_KINDS: set[str] = {k.value for k in ClaimKind}
# Claim.status is a plain string column (different claim kinds have different lifecycles -
# PLATFORM.md §6's Claim table comment); a filter value is accepted when it is a member of
# ANY lifecycle a claim can carry, including the existing ai_proposal one (PLATFORM.md §4.3).
_VALID_CLAIM_STATUSES: set[str] = (
    {s.value for s in DecisionStatus} | {s.value for s in HypothesisStatus} | {"proposed", "confirmed", "rejected"}
)


def _claim_kind_or_400(value: str) -> ClaimKind:
    try:
        return ClaimKind(value)
    except ValueError as e:
        raise ValueError(f"unknown claim kind {value!r}; expected one of {sorted(_VALID_CLAIM_KINDS)}") from e


def _claim_status_or_400(value: str) -> str:
    if value not in _VALID_CLAIM_STATUSES:
        raise ValueError(f"unknown claim status {value!r}; expected one of {sorted(_VALID_CLAIM_STATUSES)}")
    return value


def claim_summary(c: Claim) -> dict[str, Any]:
    return {
        "id": c.id, "kind": c.kind.value, "slug": c.slug, "title": c.title, "status": c.status,
        "date": c.date.isoformat() if c.date else None, "path": c.path, "has_effect": c.effect_json is not None,
    }


def list_claims(session: Session, kind: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
    """``[{id, kind, slug, title, status, date, path, has_effect}]`` (UI.md Part 1),
    filtered on ``kind`` / ``status`` when given. An unknown ``kind`` or ``status``
    raises ``ValueError`` (-> 400), validated against the closed enums (PLATFORM.md §4.3, §6)."""
    stmt = select(Claim).order_by(Claim.id)
    if kind is not None:
        stmt = stmt.where(Claim.kind == _claim_kind_or_400(kind))
    if status is not None:
        stmt = stmt.where(Claim.status == _claim_status_or_400(status))
    return [claim_summary(c) for c in session.scalars(stmt).all()]


def _effect_out(effect_json: dict[str, Any] | None) -> dict[str, Any] | None:
    if not effect_json:
        return None
    return {
        "category_code": effect_json.get("category"), "year": effect_json.get("year"),
        "value": effect_json.get("value"), "unit": effect_json.get("unit"),
    }


def claim_detail(session: Session, claim_id: int) -> dict[str, Any]:
    """One claim plus its evidence (file order), reversal condition, quantified effect and
    outgoing links (UI.md Part 1). 404 (``LookupError``) when the claim does not exist."""
    claim = session.get(Claim, claim_id)
    if claim is None:
        raise LookupError(f"claim {claim_id} not found")
    evidence = session.scalars(
        select(Evidence).where(Evidence.claim_id == claim_id).order_by(Evidence.id)
    ).all()
    links = session.scalars(
        select(ClaimLink).where(ClaimLink.from_claim_id == claim_id).order_by(ClaimLink.id)
    ).all()
    others = {l.to_claim_id: session.get(Claim, l.to_claim_id) for l in links}
    return {
        **claim_summary(claim),
        "evidence": [
            {"section": e.section.value, "text": e.text, "tag_kind": e.tag_kind.value, "tag_raw": e.tag_raw,
             "target_path": e.target_path, "resolved": e.resolved}
            for e in evidence
        ],
        "reversal_condition": claim.reversal_condition,
        "effect": _effect_out(claim.effect_json),
        "links": [
            {"relation": l.relation.value, "other_slug": others[l.to_claim_id].slug if others.get(l.to_claim_id) else None}
            for l in links
        ],
    }


def decided_effects_list(session: Session) -> list[dict[str, Any]]:
    """``GET /brain/effects`` (UI.md Part 1): every decided decision's quantified effect,
    reusing ``bridge.effects.decided_effects`` rather than re-deriving the same query."""
    return [
        {
            "claim_id": e.claim_id, "decision_slug": e.decision_slug, "decision_title": e.decision_title,
            "category_code": e.category_code, "year": e.year, "value": e.value, "unit": e.unit,
            "decided_on": e.decided_on.isoformat() if e.decided_on else None,
        }
        for e in _decided_effects(session)
    ]


def claim_impact(session: Session, claim_id: int) -> list[dict[str, Any]]:
    """The reverse of the trace (UI.md Part 1): which plan values this claim drove, newest
    scenario first, plus the default value each one displaced when the derivation recorded
    one. Reads the derivation ``run_plan`` already writes for a claim-sourced override
    (``inputs_json["default_value"]`` - see ``nvplan.services.planning.run_plan``'s
    docstring) rather than re-deriving trace logic. Empty list, never 404, when the claim
    drove nothing - only a missing claim itself is a 404."""
    claim = session.get(Claim, claim_id)
    if claim is None:
        raise LookupError(f"claim {claim_id} not found")
    cats_by_id = {c.id: c for c in category_map(session).values()}
    rows = session.scalars(select(PlanValue).where(PlanValue.claim_id == claim_id)).all()
    rows = sorted(rows, key=lambda pv: (-pv.scenario_id, pv.year))
    out: list[dict[str, Any]] = []
    for pv in rows:
        scen = session.get(Scenario, pv.scenario_id)
        cat = cats_by_id.get(pv.category_id)
        d = session.get(Derivation, pv.derivation_id)
        displaced = (d.inputs_json or {}).get("default_value") if d is not None else None
        out.append({
            "plan_value_id": pv.id,
            "scenario_kind": scen.kind.value if scen is not None else None,
            "category_code": cat.code if cat is not None else None,
            "year": pv.year,
            "value": pv.value,
            "path": pv.path.value,
            "displaced_default": float(displaced) if displaced is not None else None,
        })
    return out
