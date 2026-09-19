"""The planning run: deterministic core -> database, with a derivation for every number.

This module is the only writer of ``derivation``, ``parameter``, ``scenario``,
``plan_value`` and ``statement_line`` rows. It is append-only: a rerun inserts a
new set of scenarios / parameters / values and never updates or deletes an old
row, so every historic plan stays reproducible (PDF: "historic plans stay
reproducible").

Steps of :func:`run_plan`
-------------------------
1. actuals from the ``actual`` table (``ingest.actuals_frame``) -> wide frame
2. depreciation schedule from the investment plan CSV
3. ``fit_all`` (OTH regressed net of DEPR)
4. ``revenue_default_path``; a ``revenue_override`` (confirmed AI proposal)
   replaces those years in the base path and is recorded in the base REV
   derivation (formula "confirmed AI proposal")
5. ``project_all`` for base / best / worst with the control-table spread
6. ``build_statements`` per scenario (yaml mapping, control-table tax rate,
   yaml opening balance sheet); ``check_consistency`` must be clean
7. persist everything in ONE transaction: derivations first (topological
   order, string keys -> ids, ``parent_ids_json`` filled), then parameters,
   scenarios, plan values, statement lines.

Derivation key persistence
--------------------------
The ``derivation`` table has no key column. The ledger key (``param:PERS``,
``plan:base:REV:2027``, ``actual:REV:2025``, ``stmt:base:bs:cash:2028`` ...)
is stored as ``inputs_json["_key"]`` so the trace service can classify a node
(parameter / actual anchor / plan value ...) without heuristics. The trace
filters the ``_``-prefixed entry out of the rendered inputs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import pandas as pd
import yaml
from sqlalchemy.orm import Session

from nvplan import config
from nvplan.core import CALC_VERSION, DerivationLedger, to_wide
from nvplan.core.depreciation import capex_key, depr_key, depreciation_schedule
from nvplan.core.projector import SCENARIOS, plan_key, project_all, revenue_key
from nvplan.core.regression import (
    DEFAULT_COST_CODES,
    FitResult,
    default_revenue_key,
    fit_all,
    param_key,
    revenue_default_path,
)
from nvplan.core.statements import StatementSet, build_statements, check_consistency, load_mapping
from nvplan.db.models import (
    Derivation,
    Parameter,
    PlanPath,
    PlanValue,
    Scenario,
    ScenarioKind,
    Statement,
    StatementLine,
)
from nvplan.db.session import category_map
from nvplan.ingest.actuals import actuals_frame

__all__ = [
    "PlanRun",
    "run_plan",
    "load_control_table",
    "KEY_FIELD",
    "AI_OVERRIDE_FORMULA",
    "DECISION_OVERRIDE_FORMULA",
    "Override",
]

#: Name of the entry in ``derivation.inputs_json`` that carries the ledger key.
KEY_FIELD = "_key"
#: formula_text of a base REV derivation that was replaced by a confirmed AI proposal.
AI_OVERRIDE_FORMULA = "confirmed AI proposal"
#: formula_text of a base REV derivation that was replaced by a decided decision
#: (PLATFORM.md §7.1 - the bridge contract).
DECISION_OVERRIDE_FORMULA = "confirmed decision"


@dataclass(frozen=True)
class Override:
    """A revenue override, sourced from either a confirmed AI proposal (``ai_record_id``)
    or a decided decision (``claim_id``) - PLATFORM.md §7.1. Exactly one of the two ids
    must be set; the other stays ``None``.

    The base REV derivation's ``formula_text`` is not a field here: ``run_plan`` derives
    it from which id is set (``claim_id`` -> :data:`DECISION_OVERRIDE_FORMULA`,
    ``ai_record_id`` -> :data:`AI_OVERRIDE_FORMULA`), so there is no separate value to
    pass in or to disagree with the id that was actually set."""

    value: float
    ai_record_id: int | None = None
    claim_id: int | None = None
    label: str = ""

    def __post_init__(self) -> None:
        if (self.ai_record_id is None) == (self.claim_id is None):
            raise ValueError(
                "Override must carry exactly one of ai_record_id or claim_id, got "
                f"ai_record_id={self.ai_record_id!r} claim_id={self.claim_id!r}"
            )


def _as_override(entry: tuple[float, int] | Override) -> Override:
    """Normalise one ``revenue_override`` entry to an :class:`Override`.

    A bare ``(value, ai_record_id)`` tuple keeps its exact current meaning (an AI-sourced
    override) so every existing caller / test keeps working unchanged; an :class:`Override`
    is already validated (exactly one id) at construction and is returned as-is.
    """
    if isinstance(entry, Override):
        return entry
    value, ai_record_id = entry
    return Override(value=float(value), ai_record_id=int(ai_record_id))


#: ``year -> (value, ai_record_id)`` tuple (legacy, AI-sourced) or the widened
#: :class:`Override` (AI- or claim-sourced) - PLATFORM.md §7.1.
RevenueOverride = Mapping[int, tuple[float, int] | Override]


@dataclass
class PlanRun:
    """Ids of everything one :func:`run_plan` call inserted."""

    scenario_ids: dict[str, int] = field(default_factory=dict)  # scenario kind -> scenario.id
    parameter_ids: dict[str, int] = field(default_factory=dict)  # category code -> parameter.id
    derivation_ids: dict[str, int] = field(default_factory=dict)  # ledger key -> derivation.id
    n_plan_values: int = 0
    n_statement_lines: int = 0
    label: str = ""
    created_at: datetime | None = None

    @property
    def n_derivations(self) -> int:
        return len(self.derivation_ids)


# --------------------------------------------------------------------------- config helpers


def load_control_table(path: str | Path | None = None) -> dict[str, Any]:
    path = Path(path) if path is not None else config.DATA_DIR / "control_table.yaml"
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _spread_from_control_table(ct: Mapping[str, Any]) -> dict[str, float]:
    raw = ct.get("revenue_proposal", {}).get("scenario_spread")
    if not raw:
        raise ValueError("control_table.yaml has no revenue_proposal.scenario_spread")
    return {str(k): float(v) for k, v in raw.items()}


# --------------------------------------------------------------------------- the run


def run_plan(
    session: Session,
    *,
    window: tuple[int, int] = config.REGRESSION_WINDOW,
    plan_years: tuple[int, int] = config.PLAN_YEARS,
    created_by: str = "system",
    revenue_override: RevenueOverride | None = None,
    spread: Mapping[str, float] | None = None,
    label_suffix: str = "",
    data_dir: str | Path = config.DATA_DIR,
    investment_plan_path: str | Path | None = None,
    mapping_path: str | Path | None = None,
    control_table_path: str | Path | None = None,
) -> PlanRun:
    """Compute the plan from the actuals in ``session`` and persist it (one transaction).

    ``revenue_override`` maps ``year -> entry``, where ``entry`` is either the legacy
    ``(value, ai_record_id)`` tuple or an :class:`Override` (PLATFORM.md §7.1). Both replace
    the valorized default for that year in the base path.

    * AI-sourced (bare tuple, or an ``Override`` with ``ai_record_id``): behaves exactly as
      before - the REV plan values of that year (all scenarios: best / worst are
      ``proposed x (1 + spread)``) get ``path=ai_proposed`` and ``ai_record_id``, and the
      base REV derivation's ``formula_text`` becomes :data:`AI_OVERRIDE_FORMULA`.
    * Claim-sourced (an ``Override`` with ``claim_id``): the REV plan values of that year get
      ``path=decided`` and ``claim_id`` instead, and the base REV derivation's
      ``formula_text`` becomes :data:`DECISION_OVERRIDE_FORMULA`, with inputs
      ``{claim_id, decision_slug, proposed_value, default_value}`` (``decision_slug`` comes
      from ``Override.label``). The displaced default stays the derivation's parent either
      way, so it remains traceable.

    Every cost value stays ``cascaded`` in both cases.

    ``spread`` defaults to ``revenue_proposal.scenario_spread`` of the control table.
    Returns a :class:`PlanRun` with the ids of all inserted rows.
    """
    data_dir = Path(data_dir)
    investment_plan_path = Path(investment_plan_path or data_dir / "investment_plan.csv")
    mapping_path = Path(mapping_path or data_dir / "bs_mapping.yaml")
    control_table_path = Path(control_table_path or data_dir / "control_table.yaml")

    control_table = load_control_table(control_table_path)
    tax_rate = float(control_table["tax_rate"])
    spread = dict(spread) if spread is not None else _spread_from_control_table(control_table)
    mapping = load_mapping(mapping_path)
    opening = mapping.get("opening_balance_sheet")
    if opening is None:
        raise ValueError(f"{mapping_path} has no opening_balance_sheet")
    lo, hi = int(plan_years[0]), int(plan_years[1])
    years = list(range(lo, hi + 1))
    t0 = int(window[1])

    # ---- 1. actuals ---------------------------------------------------------
    actuals = actuals_frame(session, include_components=True)
    if actuals.empty:
        raise ValueError("no actuals in the database - ingest first")
    wide = to_wide(actuals)
    source_labels = {
        (str(r.category_code), int(r.year)): str(r.source_label) for r in actuals.itertuples(index=False)
    }

    ledger = DerivationLedger()

    # ---- 2. depreciation -----------------------------------------------------
    investment_plan = pd.read_csv(investment_plan_path)
    sched = depreciation_schedule(investment_plan, ledger=ledger)
    depr = sched.set_index("year")["depreciation"]
    if "source_label" in investment_plan.columns:  # carry the provenance of the capex anchors
        for row in investment_plan.itertuples(index=False):
            k = capex_key(int(row.year))
            d = ledger.get(k)
            ledger.add(k, d.formula_text, inputs={**d.inputs, "source_label": str(row.source_label)},
                       parameters=d.parameters, parents=d.parents, replace=True)

    # ---- 3. regression -------------------------------------------------------
    if "DEPR" not in wide.columns:
        raise ValueError("actuals have no DEPR series; OTH must be regressed net of depreciation")
    fits = fit_all(wide, DEFAULT_COST_CODES, window, ledger=ledger, depreciation=wide["DEPR"])

    # ---- 4. revenue path -----------------------------------------------------
    base = revenue_default_path(wide, window, plan_years, ledger=ledger)
    default_path = base.copy()
    anchor_key = f"actual:REV:{t0}"
    d = ledger.get(anchor_key)
    ledger.add(anchor_key, d.formula_text,
               inputs={**d.inputs, "source_label": source_labels.get(("REV", t0), "")},
               parameters=d.parameters, parents=d.parents, replace=True)

    override: dict[int, Override] = {}
    if revenue_override:
        for y, entry in revenue_override.items():
            y = int(y)
            if y not in years:
                raise ValueError(f"revenue_override year {y} outside plan years {plan_years}")
            ov = _as_override(entry)
            override[y] = ov
            base.loc[y] = float(ov.value)

    # ---- 5. projection -------------------------------------------------------
    plan = project_all(fits, base, spread, t0=t0, depreciation=depr, ledger=ledger)
    plan["ai_record_id"] = pd.array([None] * len(plan), dtype="object")
    plan["claim_id"] = pd.array([None] * len(plan), dtype="object")

    for y, ov in override.items():
        mask = (plan.category_code == "REV") & (plan.year == y)
        if ov.claim_id is not None:
            # claim-sourced (a decided decision, PLATFORM.md §7.1): the base REV derivation
            # of that year records the decision; parent = the default it replaced, exactly
            # as for a confirmed AI proposal, so the discarded default stays traceable.
            ledger.add(
                revenue_key("base", y),
                DECISION_OVERRIDE_FORMULA,
                inputs={
                    "claim_id": ov.claim_id,
                    "decision_slug": ov.label,
                    "proposed_value": ov.value,
                    "default_value": float(default_path.loc[y]),
                },
                parameters={"t": y, "touchpoint": "decision"},
                parents=(default_revenue_key(y),),
                replace=True,
            )
            plan.loc[mask, "path"] = "decided"
            plan.loc[mask, "claim_id"] = ov.claim_id
        else:
            # AI-sourced (a confirmed AI proposal): unchanged from before.
            ledger.add(
                revenue_key("base", y),
                AI_OVERRIDE_FORMULA,
                inputs={"proposed_value": ov.value, "ai_record_id": ov.ai_record_id,
                        "default_value": float(default_path.loc[y])},
                parameters={"t": y, "touchpoint": "revenue_proposal"},
                parents=(default_revenue_key(y),),
                replace=True,
            )
            plan.loc[mask, "path"] = "ai_proposed"
            plan.loc[mask, "ai_record_id"] = ov.ai_record_id

        # best / worst REV of that year are already `proposed x (1 + spread)`
        # pass-throughs (scenario_revenue_paths, registered by project_all above); what
        # each one displaced is therefore `default x (1 + spread)` for its own spread.
        # Record that as an additional input, purely additive: formula_text and parents
        # stay exactly the spread pass-through they already are (UI.md Part 1 / the
        # impact route needs a displaced default on all three rows, not just base).
        default_before = float(default_path.loc[y])
        for scenario, s in spread.items():
            k = revenue_key(scenario, y)
            d = ledger.get(k)
            ledger.add(
                k,
                d.formula_text,
                inputs={**d.inputs, "default_value": default_before * (1.0 + float(s))},
                parameters=d.parameters,
                parents=d.parents,
                replace=True,
            )

    # DEPR plan rows point at depr:{year}; statements reference plan:{scenario}:DEPR:{year}.
    # Register that pass-through so both resolve to one derivation per plan value.
    for scenario in SCENARIOS:
        for y in years:
            k = plan_key(scenario, "DEPR", y)
            ledger.add(k, "DEPR_t (component of OTH, from the investment plan)",
                       inputs={"DEPR_t": float(depr.loc[y])}, parameters={"t": y, "scenario": scenario},
                       parents=(depr_key(y),))
            mask = (plan.scenario == scenario) & (plan.category_code == "DEPR") & (plan.year == y)
            plan.loc[mask, "derivation_key"] = k
    ledger.validate()

    # ---- 6. statements -------------------------------------------------------
    statements: dict[str, StatementSet] = {}
    for scenario in SCENARIOS:
        pl_long = plan.loc[plan.scenario == scenario, ["category_code", "year", "value"]].reset_index(drop=True)
        stmts = build_statements(pl_long, investment_plan, mapping, scenario=scenario,
                                 tax_rate=tax_rate, opening=opening)
        problems = check_consistency(stmts)
        if problems:
            raise ValueError(f"statements for scenario {scenario!r} are inconsistent: {problems}")
        statements[scenario] = stmts

    # ---- 7. persist (one transaction) ---------------------------------------
    try:
        run = _persist(session, ledger, statements, fits, plan, window, created_by, label_suffix)
        session.commit()
    except Exception:
        session.rollback()
        raise
    return run


# --------------------------------------------------------------------------- persistence


def _merged_records(ledger: DerivationLedger, statements: Mapping[str, StatementSet]) -> dict[str, Any]:
    """key -> record (core Derivation | statements LineDerivation; same field names)."""
    records: dict[str, Any] = {d.key: d for d in ledger}
    for stmts in statements.values():
        for key, d in stmts.derivations.items():
            if key in records:
                raise ValueError(f"derivation key {key!r} registered by both core and statements")
            records[key] = d
    missing = {k: [p for p in d.parents if p not in records] for k, d in records.items()}
    missing = {k: v for k, v in missing.items() if v}
    if missing:
        raise KeyError(f"derivations reference parents that exist nowhere: {missing}")
    return records


def _levels(records: Mapping[str, Any]) -> list[list[str]]:
    """Keys grouped by depth (all parents of a key sit in an earlier level). Cycles raise."""
    depth: dict[str, int] = {}
    state: dict[str, int] = {}

    def visit(k: str) -> int:
        if k in depth:
            return depth[k]
        if state.get(k) == 1:
            raise ValueError(f"cycle in derivation lineage at {k!r}")
        if k not in records:
            raise KeyError(f"derivation references unknown parent {k!r}")
        state[k] = 1
        d = 0 if not records[k].parents else 1 + max(visit(p) for p in records[k].parents)
        depth[k] = d
        state[k] = 2
        return d

    for k in records:
        visit(k)
    levels: list[list[str]] = [[] for _ in range(max(depth.values(), default=-1) + 1)]
    for k, d in depth.items():
        levels[d].append(k)
    return levels


def _insert_derivations(session: Session, records: Mapping[str, Any], computed_at: datetime) -> dict[str, int]:
    """Insert every derivation, parents before children, returning key -> id."""
    ids: dict[str, int] = {}
    for level in _levels(records):
        rows: list[tuple[str, Derivation]] = []
        for key in level:
            d = records[key]
            inputs = dict(d.inputs)
            inputs[KEY_FIELD] = key
            row = Derivation(
                formula_text=d.formula_text,
                inputs_json=inputs,
                parameters_json=dict(d.parameters),
                parent_ids_json=[ids[p] for p in d.parents],  # parents are in earlier levels
                computed_at=computed_at,
            )
            session.add(row)
            rows.append((key, row))
        session.flush()
        for key, row in rows:
            ids[key] = int(row.id)
    return ids


def _persist(
    session: Session,
    ledger: DerivationLedger,
    statements: Mapping[str, StatementSet],
    fits: Mapping[str, FitResult],
    plan: pd.DataFrame,
    window: tuple[int, int],
    created_by: str,
    label_suffix: str,
) -> PlanRun:
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    cats = category_map(session)
    records = _merged_records(ledger, statements)
    ids = _insert_derivations(session, records, now)

    # parameters: immutable, always a new row
    parameter_ids: dict[str, int] = {}
    for code, fit in fits.items():
        p = Parameter(
            category_id=cats[code].id,
            alpha=fit.alpha,
            beta=fit.beta,
            r_squared=fit.r_squared,
            valorization_rate=fit.valorization_rate,
            window_from=fit.window_from,
            window_to=fit.window_to,
            computed_at=now,
            calc_version=CALC_VERSION,
            derivation_id=ids[param_key(code)],
        )
        session.add(p)
        session.flush()
        parameter_ids[code] = int(p.id)

    # scenarios: one per kind per run
    scenario_ids: dict[str, int] = {}
    label = f"{stamp}{label_suffix}"
    for kind in SCENARIOS:
        s = Scenario(kind=ScenarioKind(kind), label=f"{kind} {label}", created_by=created_by, created_at=now)
        session.add(s)
        session.flush()
        scenario_ids[kind] = int(s.id)

    # plan values
    n_pv = 0
    for row in plan.itertuples(index=False):
        rec_id = row.ai_record_id
        claim_id = row.claim_id
        session.add(
            PlanValue(
                scenario_id=scenario_ids[row.scenario],
                category_id=cats[row.category_code].id,
                year=int(row.year),
                value=float(row.value),
                path=PlanPath(row.path),
                derivation_id=ids[row.derivation_key],
                ai_record_id=None if rec_id is None or pd.isna(rec_id) else int(rec_id),
                claim_id=None if claim_id is None or pd.isna(claim_id) else int(claim_id),
            )
        )
        n_pv += 1

    # statement lines
    n_sl = 0
    for kind, stmts in statements.items():
        for stmt, frame in (("pl", stmts.pl), ("bs", stmts.bs), ("cf", stmts.cf)):
            has_ref = "mapping_ref" in frame.columns
            for row in frame.itertuples(index=False):
                ref = str(row.mapping_ref) if has_ref else f"pl.lines.{row.line_code}"
                session.add(
                    StatementLine(
                        scenario_id=scenario_ids[kind],
                        statement=Statement(stmt),
                        line_code=str(row.line_code),
                        year=int(row.year),
                        value=float(row.value),
                        mapping_ref=ref,
                        derivation_id=ids[row.derivation_key],
                    )
                )
                n_sl += 1
    session.flush()

    return PlanRun(
        scenario_ids=scenario_ids,
        parameter_ids=parameter_ids,
        derivation_ids=ids,
        n_plan_values=n_pv,
        n_statement_lines=n_sl,
        label=label,
        created_at=now,
    )
