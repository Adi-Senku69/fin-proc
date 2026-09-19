"""Click a number, get its full lineage (PDF section 6).

Every ``plan_value`` / ``statement_line`` / ``parameter`` row points at a
``derivation`` row; derivations point at their parents via
``parent_ids_json``. :func:`trace_derivation` walks that graph into a
:class:`TraceNode` tree, attaching the DB row that owns each derivation:

* ``plan_value``      -> label "Personnel costs · 2028 · Base", path, value,
                          the ``ai`` block when the value is ``ai_proposed``, and the
                          ``claim`` block (decision title/status/evidence, PLATFORM.md
                          §7.1) when the value carries a ``claim_id`` (``path=decided``)
* ``statement_line``  -> "BS cash · 2028 · Base"
* ``parameter``       -> the regression row (alpha, beta, R², v) + the points
* ``actual``          -> the source anchor (``actual:REV:2025``) with its
                          ``source_label`` (carries ILLUSTRATIVE for dummy data)
* ``derivation``      -> any other intermediate (default revenue path, capex,
                          depreciation, pass-throughs)

Guards: a derivation already expanded elsewhere in the same tree is shown as a
reference (``children=[]``, ``ref=True``) so shared inputs (the revenue path,
the parameters) do not blow the tree up; a derivation on the current ancestor
path is a cycle and is cut; ``max_depth`` cuts the rest (``truncated=True``).

:func:`render_trace` prints the PDF's tree layout. Numbers are formatted with
thousands separators and one decimal for display only - nothing in the data
is rounded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from nvplan import config
from nvplan.db.models import (
    Actual,
    AiRecord,
    Category,
    Derivation,
    Parameter,
    PlanValue,
    Scenario,
    ScenarioKind,
    StatementLine,
)

# ``provenance`` is the shared provenance core (PLATFORM.md §7.1): importing it from here is
# intended, not a layering violation. In a finance-only database the claim/evidence tables
# don't exist at all - every lookup below is wrapped so that is never fatal to a trace.
from provenance import Claim, Evidence

__all__ = [
    "TraceNode",
    "trace_plan_value",
    "trace_statement_line",
    "trace_derivation",
    "find_plan_value",
    "render_trace",
]

KEY_FIELD = "_key"
_PRETTY_CODES = {"REV": "Revenue", "MAT": "Material costs", "EXT": "External services",
                 "PERS": "Personnel costs", "OTH": "Other costs", "DEPR": "Depreciation"}


@dataclass
class TraceNode:
    kind: str  # plan_value | statement_line | parameter | derivation | actual | ai_record
    label: str
    value: float | None = None
    path: str | None = None
    formula_text: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    inputs: dict[str, Any] = field(default_factory=dict)
    source_label: str | None = None
    ai: dict[str, Any] | None = None
    claim: dict[str, Any] | None = None
    children: list[TraceNode] = field(default_factory=list)
    # bookkeeping
    key: str | None = None
    derivation_id: int | None = None
    ref_id: int | None = None  # id of the owning plan_value / statement_line / parameter / actual row
    depth: int = 0
    ref: bool = False  # already expanded elsewhere in this tree
    truncated: bool = False  # cut by max_depth or a cycle
    meta: dict[str, Any] = field(default_factory=dict)

    def walk(self):
        """Yield every node of the tree (pre-order)."""
        yield self
        for c in self.children:
            yield from c.walk()

    def find(self, **attrs) -> TraceNode | None:
        """First node whose attributes / meta match all ``attrs``."""
        for n in self.walk():
            if all((getattr(n, k, None) == v) or (n.meta.get(k) == v) for k, v in attrs.items()):
                return n
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "label": self.label, "value": self.value, "path": self.path,
            "formula_text": self.formula_text, "parameters": self.parameters, "inputs": self.inputs,
            "source_label": self.source_label, "ai": self.ai, "claim": self.claim, "key": self.key,
            "derivation_id": self.derivation_id, "ref_id": self.ref_id, "ref": self.ref,
            "truncated": self.truncated, "meta": self.meta,
            "children": [c.to_dict() for c in self.children],
        }


# --------------------------------------------------------------------------- lookups


class _Lookup:
    """Per-trace cache of the rows that own derivations."""

    def __init__(self, session: Session) -> None:
        self.s = session
        self.cats: dict[int, Category] = {c.id: c for c in session.scalars(select(Category)).all()}
        self.cats_by_code: dict[str, Category] = {c.code: c for c in self.cats.values()}
        self.scenarios: dict[int, Scenario] = {}

    def scenario(self, sid: int) -> Scenario:
        if sid not in self.scenarios:
            self.scenarios[sid] = self.s.get(Scenario, sid)
        return self.scenarios[sid]

    def plan_value(self, derivation_id: int) -> PlanValue | None:
        return self.s.scalars(select(PlanValue).where(PlanValue.derivation_id == derivation_id)).first()

    def statement_line(self, derivation_id: int) -> StatementLine | None:
        return self.s.scalars(select(StatementLine).where(StatementLine.derivation_id == derivation_id)).first()

    def parameter(self, derivation_id: int) -> Parameter | None:
        return self.s.scalars(select(Parameter).where(Parameter.derivation_id == derivation_id)).first()

    def actual(self, code: str, year: int) -> Actual | None:
        cat = self.cats_by_code.get(code)
        if cat is None:
            return None
        return self.s.scalars(select(Actual).where(Actual.category_id == cat.id, Actual.year == year)).first()

    def claim_block(self, claim_id: int) -> dict[str, Any] | None:
        """Best-effort decision info for ``claim_id`` (PLATFORM.md §7.1): title, slug,
        status, decided date, evidence rows with their provenance tags, and the reversal
        condition when the index carries one.

        Must degrade gracefully: in a finance-only database the ``claim``/``evidence``
        tables don't exist at all, and even in a joint database the row may be missing
        (e.g. a stale ``claim_id`` after the brain index was rebuilt). Either case yields
        ``None`` - a trace must never fail because the brain index is absent or stale.
        """
        try:
            claim = self.s.get(Claim, claim_id)
            if claim is None:
                return None
            evidence = self.s.scalars(
                select(Evidence).where(Evidence.claim_id == claim_id).order_by(Evidence.id)
            ).all()
            return {
                "claim_id": claim.id,
                "slug": claim.slug,
                "title": claim.title,
                "status": claim.status,
                "decided_on": claim.date.isoformat() if claim.date else None,
                "evidence": [{"text": e.text, "tag_raw": e.tag_raw} for e in evidence],
                # Not a column on today's Claim model (PLATFORM.md §7.1: "if the index
                # carries one") - stays None until/unless one is added there.
                "reversal_condition": getattr(claim, "reversal_condition", None),
            }
        except Exception:
            # Missing table (finance-only DB) or any other lookup failure: no claim block,
            # never raise. Roll back so the session stays usable for the rest of the trace.
            try:
                self.s.rollback()
            except Exception:
                pass
            return None


def _ai_block(rec: AiRecord) -> dict[str, Any]:
    return {
        "ai_record_id": rec.id,
        "touchpoint": rec.touchpoint.value,
        "status": rec.status.value,
        "model_version": rec.model_version,
        "proposed_value": rec.proposed_value,
        "year": rec.year,
        "prompt_text": rec.prompt_text,
        "response_text": rec.response_text,
        "rationale": rec.rationale,
        "confirmed_by": rec.confirmed_by,
        "confirmed_at": rec.confirmed_at.isoformat() if rec.confirmed_at else None,
        "created_at": rec.created_at.isoformat() if rec.created_at else None,
    }


def _scenario_title(kind: ScenarioKind | str) -> str:
    k = kind.value if isinstance(kind, ScenarioKind) else str(kind)
    return k.capitalize()


# --------------------------------------------------------------------------- tree building


def _node_for(d: Derivation, lk: _Lookup, depth: int) -> TraceNode:
    inputs = dict(d.inputs_json or {})
    key = inputs.pop(KEY_FIELD, None)
    params = dict(d.parameters_json or {})
    node = TraceNode(kind="derivation", label=key or f"derivation #{d.id}", formula_text=d.formula_text,
                     parameters=params, inputs=inputs, key=key, derivation_id=d.id, depth=depth)

    pv = lk.plan_value(d.id)
    if pv is not None:
        cat, scen = lk.cats[pv.category_id], lk.scenario(pv.scenario_id)
        pretty = _PRETTY_CODES.get(cat.code, cat.name)
        node.kind = "plan_value"
        node.label = f"{pretty} · {pv.year} · {_scenario_title(scen.kind)}"
        node.value, node.path, node.ref_id = pv.value, pv.path.value, pv.id
        node.meta = {"category_code": cat.code, "year": pv.year, "scenario_kind": scen.kind.value,
                     "scenario_id": scen.id, "plan_value_id": pv.id}
        if pv.ai_record_id is not None:
            rec = lk.s.get(AiRecord, pv.ai_record_id)
            if rec is not None:
                node.ai = _ai_block(rec)
        if pv.claim_id is not None:
            node.claim = lk.claim_block(pv.claim_id)
        return node

    sl = lk.statement_line(d.id)
    if sl is not None:
        scen = lk.scenario(sl.scenario_id)
        node.kind = "statement_line"
        node.label = f"{sl.statement.value.upper()} {sl.line_code} · {sl.year} · {_scenario_title(scen.kind)}"
        node.value, node.ref_id = sl.value, sl.id
        node.meta = {"statement": sl.statement.value, "line_code": sl.line_code, "year": sl.year,
                     "scenario_kind": scen.kind.value, "scenario_id": scen.id, "mapping_ref": sl.mapping_ref,
                     "statement_line_id": sl.id}
        return node

    prm = lk.parameter(d.id)
    if prm is not None:
        cat = lk.cats[prm.category_id]
        node.kind = "parameter"
        node.label = f"Parameters {cat.code} ({cat.name}) · OLS {prm.window_from}-{prm.window_to} · {prm.calc_version}"
        node.ref_id = prm.id
        node.parameters = {"alpha": prm.alpha, "beta": prm.beta, "v": prm.valorization_rate,
                           "r_squared": prm.r_squared, **{k: v for k, v in params.items()
                                                          if k not in ("alpha", "beta", "v", "r_squared")}}
        node.meta = {"category_code": cat.code, "parameter_id": prm.id, "window_from": prm.window_from,
                     "window_to": prm.window_to, "calc_version": prm.calc_version}
        return node

    if key:
        parts = key.split(":")
        if parts[0] == "param" and len(parts) == 2:  # e.g. param:REV (growth rate, no parameter row)
            code = parts[1]
            cat = lk.cats_by_code.get(code)
            node.kind = "parameter"
            node.label = f"Parameters {code} ({cat.name if cat else code}) · derived"
            node.meta = {"category_code": code}
        elif parts[0] == "actual" and len(parts) == 3:
            code, year = parts[1], int(parts[2])
            act = lk.actual(code, year)
            node.kind = "actual"
            node.label = f"{_PRETTY_CODES.get(code, code)} · {year} · actual"
            node.value = act.value if act is not None else inputs.get("value")
            node.source_label = act.source_label if act is not None else inputs.get("source_label")
            node.ref_id = act.id if act is not None else None
            node.meta = {"category_code": code, "year": year}
        elif parts[0] == "capex" and len(parts) == 2:
            node.kind = "actual"
            node.label = f"Capex · {parts[1]} · investment plan"
            node.value = inputs.get("capex")
            node.source_label = inputs.get("source_label", "investment_plan.csv")
            node.meta = {"year": int(parts[1])}
        elif parts[0] == "depr" and len(parts) == 2:
            node.label = f"Depreciation charge · {parts[1]}"
            node.meta = {"year": int(parts[1])}
        elif parts[0] == "plan" and len(parts) == 4:
            node.label = f"{_PRETTY_CODES.get(parts[2], parts[2])} · {parts[3]} · {parts[1]} path"
            node.meta = {"category_code": parts[2], "year": int(parts[3]), "scenario_kind": parts[1]}
    return node


def trace_derivation(session: Session, derivation_id: int, *, max_depth: int = 12) -> TraceNode:
    """Lineage tree rooted at ``derivation_id`` following ``parent_ids_json``."""
    lk = _Lookup(session)
    expanded: set[int] = set()

    def build(did: int, depth: int, ancestors: frozenset[int]) -> TraceNode:
        d = session.get(Derivation, did)
        if d is None:
            raise LookupError(f"derivation {did} not found")
        node = _node_for(d, lk, depth)
        if did in ancestors:  # cycle (should not exist; the writer rejects them)
            node.truncated = True
            node.label += "  (cycle cut)"
            return node
        if did in expanded:
            node.ref = True
            return node
        expanded.add(did)
        parents = list(d.parent_ids_json or [])
        if not parents:
            return node
        if depth >= max_depth:
            node.truncated = True
            return node
        for pid in parents:
            node.children.append(build(int(pid), depth + 1, ancestors | {did}))
        return node

    return build(int(derivation_id), 0, frozenset())


def trace_plan_value(session: Session, plan_value_id: int, *, max_depth: int = 12) -> TraceNode:
    pv = session.get(PlanValue, plan_value_id)
    if pv is None:
        raise LookupError(f"plan_value {plan_value_id} not found")
    return trace_derivation(session, pv.derivation_id, max_depth=max_depth)


def trace_statement_line(session: Session, statement_line_id: int, *, max_depth: int = 12) -> TraceNode:
    sl = session.get(StatementLine, statement_line_id)
    if sl is None:
        raise LookupError(f"statement_line {statement_line_id} not found")
    return trace_derivation(session, sl.derivation_id, max_depth=max_depth)


def find_plan_value(
    session: Session,
    *,
    scenario_kind: str | ScenarioKind,
    category_code: str,
    year: int,
    scenario_id: int | None = None,
) -> PlanValue:
    """The plan value for (kind, code, year) in the LATEST scenario of that kind (or ``scenario_id``)."""
    kind = scenario_kind if isinstance(scenario_kind, ScenarioKind) else ScenarioKind(scenario_kind)
    cat = session.scalar(select(Category).where(Category.code == category_code))
    if cat is None:
        raise LookupError(f"unknown category {category_code!r}")
    if scenario_id is None:
        scenario_id = session.scalar(
            select(Scenario.id).where(Scenario.kind == kind).order_by(Scenario.id.desc()).limit(1)
        )
        if scenario_id is None:
            raise LookupError(f"no scenario of kind {kind.value!r}")
    pv = session.scalar(
        select(PlanValue).where(PlanValue.scenario_id == scenario_id, PlanValue.category_id == cat.id,
                                PlanValue.year == int(year))
    )
    if pv is None:
        raise LookupError(f"no plan value for {kind.value}/{category_code}/{year} in scenario {scenario_id}")
    return pv


def find_statement_line(
    session: Session, *, scenario_kind: str | ScenarioKind, statement: str, line_code: str, year: int,
    scenario_id: int | None = None,
) -> StatementLine:
    """Statement line for (kind, statement, line, year) in the latest scenario of that kind."""
    from nvplan.db.models import Statement

    kind = scenario_kind if isinstance(scenario_kind, ScenarioKind) else ScenarioKind(scenario_kind)
    if scenario_id is None:
        scenario_id = session.scalar(
            select(Scenario.id).where(Scenario.kind == kind).order_by(Scenario.id.desc()).limit(1)
        )
        if scenario_id is None:
            raise LookupError(f"no scenario of kind {kind.value!r}")
    sl = session.scalar(
        select(StatementLine).where(StatementLine.scenario_id == scenario_id,
                                    StatementLine.statement == Statement(statement),
                                    StatementLine.line_code == line_code, StatementLine.year == int(year))
    )
    if sl is None:
        raise LookupError(f"no statement line {statement}/{line_code}/{year} in scenario {scenario_id}")
    return sl


# --------------------------------------------------------------------------- rendering


def _fmt(v: Any) -> str:
    if isinstance(v, bool) or v is None:
        return str(v)
    if isinstance(v, int):
        return f"{v:,}" if abs(v) >= 10000 else str(v)
    if isinstance(v, float):
        return f"{v:,.1f}" if abs(v) >= 100 else f"{v:.4f}"
    if isinstance(v, (list, dict)):
        return f"<{len(v)} entries>"
    return str(v)


def _money(v: float | None) -> str:
    return "" if v is None else f"{v:,.1f} k€"


_PARAM_ORDER = ("alpha", "beta", "v", "r_squared", "g", "spread", "t0", "t", "n", "window_from", "window_to",
                "tax_rate", "dso_days", "dpo_days", "inventory_days", "days_per_year", "life_years")
_PARAM_NAMES = {"r_squared": "R²"}


def _params_line(params: dict[str, Any]) -> str:
    keys = [k for k in _PARAM_ORDER if k in params] + [k for k in params if k not in _PARAM_ORDER]
    return " ".join(f"{_PARAM_NAMES.get(k, k)}={_fmt(params[k])}" for k in keys if not k.startswith("_"))


def _inputs_line(inputs: dict[str, Any]) -> str:
    return "; ".join(f"{k} = {_fmt(v)}" for k, v in inputs.items() if not k.startswith("_"))


def render_trace(node: TraceNode, *, indent: int = 0, width: int = 60, _child: bool = False) -> str:
    """The PDF's tree layout (see module doc). ``indent`` = leading spaces of the root line;
    ``width`` = column (relative to the indent) where the value is printed."""
    pad = " " * indent
    sub = pad + "   └─ "
    head = node.label
    if node.ref:
        head += "  (see above)"
    if node.truncated:
        head += "  (…)"
    money = _money(node.value)
    line = f"{pad}{'└─ ' if _child else ''}{head}"
    if money:
        line = f"{line:<{max(indent + width, len(line) + 2)}}{money}"
    out = [line]
    if node.ref:
        return "\n".join(out)
    if node.path:
        out.append(f"{sub}path: {node.path}")
    if node.formula_text:
        out.append(f"{sub}formula: {node.formula_text}")
    if node.parameters:
        out.append(f"{sub}parameters: {_params_line(node.parameters)}")
    if node.kind == "parameter" and isinstance(node.inputs.get("points"), list):
        pts = node.inputs["points"]
        out.append(f"{sub}regression points ({len(pts)}):")
        for p in pts:
            bits = "  ".join(f"{k}={_fmt(v)}" for k, v in p.items())
            out.append(f"{pad}        · {bits}")
    inputs = {k: v for k, v in node.inputs.items() if not k.startswith("_") and k != "points"}
    if inputs or node.children:
        out.append(f"{sub}inputs: {_inputs_line(inputs)}".rstrip())
        for child in node.children:
            out.append(render_trace(child, indent=indent + 8, width=width, _child=True))
    if node.ai:
        ai = node.ai
        out.append(f"{sub}ai: touchpoint={ai['touchpoint']} status={ai['status']} model={ai['model_version']} "
                   f"confirmed_by={ai['confirmed_by']} confirmed_at={ai['confirmed_at']}")
        out.append(f"{pad}        · rationale: {ai['rationale']}")
        out.append(f"{pad}        · prompt (verbatim):")
        for ln in str(ai["prompt_text"]).splitlines() or [""]:
            out.append(f"{pad}          | {ln}")
    if node.claim:
        c = node.claim
        out.append(f"{sub}claim: title={c['title']!r} status={c['status']} slug={c['slug']} "
                   f"decided_on={c['decided_on']}")
        if c.get("reversal_condition"):
            out.append(f"{pad}        · reversal condition: {c['reversal_condition']}")
        for e in c["evidence"]:
            out.append(f"{pad}        · evidence [{e['tag_raw']}]: {e['text']}")
    if node.kind == "actual":
        src = node.source_label or "?"
        flag = f"  [{config.ILLUSTRATIVE_LABEL}]" if config.ILLUSTRATIVE_LABEL in str(src) else ""
        what = " · ".join(str(node.meta.get(k)) for k in ("category_code", "year") if node.meta.get(k) is not None)
        out.append(f"{sub}source data: {what + ' · ' if what else ''}source_label={src}{flag}")
    return "\n".join(out)
