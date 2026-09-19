"""LangChain tools over the planning database for the AI touchpoints.

Read tools (``make_read_tools``) only SELECT. Write tools (``make_write_tools``)
may insert exactly two things: ``external_note`` rows with ``source=ai_scan``
and one ``ai_record`` row with ``status=proposed``. Nothing here touches
``plan_value``, ``statement_line``, ``actual``, ``parameter``, ``derivation``
or ``scenario`` (tests/test_ai_guardrails.py checks the row counts).

Every tool opens its own session, commits (write tools) and returns a JSON
string, because tool results become model-visible text. Row-oriented results
(``get_actuals``) are printed one row per line: a result above the eviction
threshold is offloaded to ``/large_tool_results/<id>`` by the context middleware
and read back with the line-based ``read_file(offset, limit)``.

The plan-vs-actual arithmetic behind ``get_plan_vs_actual`` is
``nvplan.ai.figures.plan_vs_actual`` - the same function the deviation
entrypoint uses to cross-check the model's figures (``figures.check_explanation``).

Run context
-----------
``AiRunContext`` is the mutable handle shared between an entrypoint in
``agents.py`` and the write tools of one run:

* the entrypoint fills ``touchpoint``, ``model_version``, ``prompt_text``,
  ``scenario_id``, ``year`` and ``default_value`` before invoking the agent;
* ``record_revenue_proposal`` validates against the control table and the
  ``default_value`` from the context, inserts the ``ai_record`` and stores its
  id in ``ai_record_id`` (a second call in the same run updates that row);
* ``record_external_note`` appends the new note ids to ``note_ids``; the
  env-scan entrypoint inserts its ``ai_record`` after the run and back-fills
  ``external_note.ai_record_id`` for those ids.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Callable

import yaml
from langchain_core.tools import BaseTool, tool
from sqlalchemy import select
from sqlalchemy.orm import Session

from nvplan.ai.figures import _categories, _latest_parameters, _scenario_by_kind, plan_vs_actual  # noqa: F401 (re-exported)
from nvplan.config import DATA_DIR
from nvplan.core import DerivationLedger, to_wide
from nvplan.core.backtest import run_backtest
from nvplan.db.models import (
    Actual,
    AiRecord,
    AiStatus,
    Category,
    ExternalNote,
    NoteSource,
    PlanValue,
    Scenario,
    Touchpoint,
)
from nvplan.ingest.actuals import actuals_frame
from nvplan.services.trace import find_plan_value, trace_plan_value

# The brain-side tables (PLATFORM.md §6) are a separate, independently-importable package;
# importing them here is the same choice nvplan.services.trace already makes (its module
# doc) so the conversational assistant (UI.md Part 3) can offer "brain-side equivalents" of
# the plan tools. Every claim/evidence query below degrades to an error string rather than
# raising when the tables don't exist (a finance-only database) - see ``_claims_unavailable``.
from provenance import Claim, ClaimKind, Evidence

SessionFactory = Callable[[], Session]

ENV_FRAMEWORK_PATH = DATA_DIR / "env_framework.yaml"
CONTROL_TABLE_PATH = DATA_DIR / "control_table.yaml"

# The only tool names allowed to write anything. Checked by the guardrail test.
ALLOWED_WRITE_TOOLS: frozenset[str] = frozenset({"record_external_note", "record_revenue_proposal"})


@dataclass
class AiRunContext:
    """State shared between one entrypoint run and its write tools (see module doc)."""

    touchpoint: Touchpoint | None = None
    model_version: str = "unknown"
    prompt_text: str = ""
    scenario_id: int | None = None
    year: int | None = None
    default_value: float | None = None
    ai_record_id: int | None = None
    note_ids: list[int] = field(default_factory=list)
    # One entry per model call, appended by nvplan.ai.audit.ContextAuditMiddleware and
    # persisted as ai_record.call_log_json (the literal per-call prompt/response record).
    call_log: list[dict[str, Any]] = field(default_factory=list)
    # Real token usage summed over every call of the run, maintained by the same middleware
    # alongside call_log (empty with the fake model, which reports no usage_metadata). Recomputable
    # from a persisted log with nvplan.ai.audit.total_usage(call_log).
    total_usage: dict[str, int] = field(default_factory=dict)


# --------------------------------------------------------------------------- helpers


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _err(msg: str) -> str:
    return _dumps({"error": msg})


def _dumps_rows(meta: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    """JSON with one compact row per line (line-addressable after eviction, see module doc)."""
    head = ", ".join(f"{json.dumps(k)}: {json.dumps(v, ensure_ascii=False, default=str)}" for k, v in meta.items())
    body = ",\n".join(" " + json.dumps(r, ensure_ascii=False, default=str) for r in rows)
    return "{" + head + (", " if head else "") + '"rows": [\n' + body + "\n]}"


def load_env_framework() -> dict[str, Any]:
    with ENV_FRAMEWORK_PATH.open() as fh:
        return yaml.safe_load(fh)


def load_control_table() -> dict[str, Any]:
    with CONTROL_TABLE_PATH.open() as fh:
        return yaml.safe_load(fh)


def _category_by_code(session: Session, code: str) -> Category | None:
    return session.scalar(select(Category).where(Category.code == code.strip().upper()))


def _claims_unavailable(session: Session, exc: Exception) -> str:
    """A finance-only database has no claim/evidence tables; degrade to an error string
    (never raise - the same graceful-degradation discipline as
    ``nvplan.services.trace._Lookup.claim_block``) and roll back so the session stays usable."""
    try:
        session.rollback()
    except Exception:  # noqa: BLE001
        pass
    return _err(f"brain tables not available in this database: {exc}")


def _note_dict(n: ExternalNote, cats: dict[int, Category]) -> dict[str, Any]:
    return {
        "id": n.id,
        "category_code": cats[n.category_id].code if n.category_id else None,
        "year": n.year,
        "text": n.text,
        "author": n.author,
        "source": n.source.value,
        "ai_record_id": n.ai_record_id,
    }


# --------------------------------------------------------------------------- read tools


def make_read_tools(session_factory: SessionFactory) -> list[BaseTool]:
    """Read-only tools. Each opens one session, selects, closes."""

    @tool
    def get_actuals(category_code: str | None = None) -> str:
        """Historical actuals per category and year (k EUR). Optional filter by category code
        (REV, MAT, EXT, PERS, OTH; DEPR is the depreciation component of OTH)."""
        with session_factory() as s:
            cats = _categories(s)
            stmt = select(Actual).order_by(Actual.category_id, Actual.year)
            if category_code:
                cat = _category_by_code(s, category_code)
                if cat is None:
                    return _err(f"unknown category code {category_code!r}")
                stmt = stmt.where(Actual.category_id == cat.id)
            rows = [
                {"category_code": cats[a.category_id].code, "year": a.year, "value": a.value, "source": a.source_label}
                for a in s.scalars(stmt).all()
            ]
        return _dumps_rows({"unit": "k EUR"}, rows)

    @tool
    def get_parameters() -> str:
        """Latest regression parameters per cost category: alpha (fixed part), beta (variable
        rate on revenue), R^2, valorization rate v and the historical window used.
        ``parameter_id`` is the id to cite one of these figures (ref kind "parameter")."""
        with session_factory() as s:
            cats = _categories(s)
            rows = [
                {
                    "category_code": cats[p.category_id].code,
                    "parameter_id": p.id,
                    "alpha": p.alpha,
                    "beta": p.beta,
                    "r_squared": p.r_squared,
                    "valorization_rate": p.valorization_rate,
                    "window": [p.window_from, p.window_to],
                    "calc_version": p.calc_version,
                    "derivation_id": p.derivation_id,
                }
                for p in _latest_parameters(s).values()
            ]
        return _dumps({"model": "PlanCost = alpha*(1+v)^(t-t0) + beta*PlanRevenue", "rows": rows})

    @tool
    def get_plan_values(scenario_kind: str, category_code: str | None = None) -> str:
        """Plan values (k EUR) for a scenario (best/base/worst), optionally one category -
        the plan grid. ``plan_value_id`` is the id to cite one of these figures (ref kind
        "plan_value")."""
        with session_factory() as s:
            scenario = _scenario_by_kind(s, scenario_kind)
            if scenario is None:
                return _err(f"no scenario of kind {scenario_kind!r}")
            cats = _categories(s)
            stmt = select(PlanValue).where(PlanValue.scenario_id == scenario.id).order_by(PlanValue.category_id, PlanValue.year)
            if category_code:
                cat = _category_by_code(s, category_code)
                if cat is None:
                    return _err(f"unknown category code {category_code!r}")
                stmt = stmt.where(PlanValue.category_id == cat.id)
            rows = [
                {
                    "plan_value_id": pv.id,
                    "category_code": cats[pv.category_id].code,
                    "year": pv.year,
                    "value": pv.value,
                    "path": pv.path.value,
                    "derivation_id": pv.derivation_id,
                    "ai_record_id": pv.ai_record_id,
                }
                for pv in s.scalars(stmt).all()
            ]
        return _dumps({"scenario": scenario.kind.value, "unit": "k EUR", "rows": rows})

    @tool
    def get_plan_value(scenario_kind: str, category_code: str, year: int) -> str:
        """One plan value (k EUR) with its id - the precise citation lookup (ref kind
        "plan_value", id=plan_value_id) for a single category/year, in the latest scenario of
        ``scenario_kind`` (best/base/worst)."""
        with session_factory() as s:
            try:
                pv = find_plan_value(s, scenario_kind=scenario_kind, category_code=category_code.strip().upper(), year=int(year))
            except LookupError as e:
                return _err(str(e))
            cats = _categories(s)
            return _dumps(
                {
                    "plan_value_id": pv.id,
                    "category_code": cats[pv.category_id].code,
                    "year": pv.year,
                    "value": pv.value,
                    "path": pv.path.value,
                    "unit": "k EUR",
                    "derivation_id": pv.derivation_id,
                    "ai_record_id": pv.ai_record_id,
                    "claim_id": pv.claim_id,
                }
            )

    @tool
    def get_trace(plan_value_id: int, max_depth: int = 6) -> str:
        """The lineage tree of one plan value (root = the plan value itself, ancestors =
        parameters / decisions / actuals / other derivations it was built from). Every node
        carries the id needed to cite it: a "plan_value" node -> ref_id, a "parameter" node ->
        ref_id, any node (incl. an "actual"/"derivation" node) -> derivation_id (a real
        ``derivation`` row, citable as ref kind "derivation"), and a node with a non-null
        "claim" block -> claim["claim_id"] (citable as ref kind "claim")."""
        with session_factory() as s:
            try:
                node = trace_plan_value(s, int(plan_value_id), max_depth=max_depth)
            except LookupError as e:
                return _err(str(e))
            return _dumps(node.to_dict())

    @tool
    def get_backtest_summary(window_len: int = 5) -> str:
        """Backtest verdict (within/marginal/missed against the MAPE thresholds) per category
        and horizon. This is a rolling analysis recomputed on demand, not a stored row - it has
        no id to cite. Never state one of these numbers as a figure; describe the verdict in
        words only (e.g. "within threshold", "missed at the 2-year horizon")."""
        with session_factory() as s:
            actuals = actuals_frame(s, include_components=True)
            if actuals.empty:
                return _err("no actuals in the database - ingest first")
            wide = to_wide(actuals)
            if "DEPR" not in wide.columns:
                return _err("actuals have no DEPR series")
            res = run_backtest(
                wide, ledger=DerivationLedger(), window_len=window_len, horizons=(1, 2, 3), depreciation=wide["DEPR"]
            )
            summary = res.summary.to_dict(orient="records")
            for row in summary:
                for k, v in list(row.items()):
                    if isinstance(v, float) and not math.isfinite(v):
                        row[k] = None
        return _dumps(
            {
                "n_cases": len(res.cases),
                "thresholds": {"mape_within": res.threshold_low, "mape_marginal": res.threshold_high},
                "summary": summary,
                "markdown": res.to_markdown(),
                "note": "no citable id - describe the verdict, never the bare MAPE number",
            }
        )

    @tool
    def get_decisions(status: str | None = None) -> str:
        """The brain's decision list: claim_id, slug, title, status, date, has_effect - the
        brain-side equivalent of the plan grid. Use ``get_decision`` for one decision's
        evidence and tags, and ``get_claim_impact`` for the plan values it drove. A decision's
        title/status is citable as a "claim" segment; its quantified effect (when
        has_effect is true) is citable as a figure with ref kind "claim", id=claim_id."""
        with session_factory() as s:
            try:
                stmt = select(Claim).where(Claim.kind == ClaimKind.decision).order_by(Claim.id)
                claims = s.scalars(stmt).all()
            except Exception as e:  # noqa: BLE001 - missing brain tables in a finance-only DB
                return _claims_unavailable(s, e)
            rows = [
                {
                    "claim_id": c.id,
                    "slug": c.slug,
                    "title": c.title,
                    "status": c.status,
                    "date": c.date.isoformat() if c.date else None,
                    "has_effect": c.effect_json is not None,
                }
                for c in claims
                if status is None or c.status == status
            ]
        return _dumps({"rows": rows})

    @tool
    def get_decision(claim_id: int) -> str:
        """One decision (or any brain claim) with its evidence rows (each carrying its
        provenance tag - the "tags" of the demo UI), reversal condition and quantified effect.
        Cite kind="claim", id=claim_id for a claim segment; the effect's value (when present)
        is also citable as ref kind "claim", id=claim_id for a figure segment."""
        with session_factory() as s:
            try:
                claim = s.get(Claim, int(claim_id))
            except Exception as e:  # noqa: BLE001 - missing brain tables in a finance-only DB
                return _claims_unavailable(s, e)
            if claim is None:
                return _err(f"claim {claim_id} not found")
            evidence = s.scalars(select(Evidence).where(Evidence.claim_id == claim.id).order_by(Evidence.id)).all()
            ej = claim.effect_json or {}
            effect = (
                {"category_code": ej.get("category"), "year": ej.get("year"), "value": ej.get("value"), "unit": ej.get("unit")}
                if claim.effect_json
                else None
            )
            return _dumps(
                {
                    "claim_id": claim.id,
                    "kind": claim.kind.value,
                    "slug": claim.slug,
                    "title": claim.title,
                    "status": claim.status,
                    "date": claim.date.isoformat() if claim.date else None,
                    "reversal_condition": claim.reversal_condition,
                    "effect": effect,
                    "evidence": [
                        {"section": e.section.value, "text": e.text, "tag_kind": e.tag_kind.value, "tag_raw": e.tag_raw}
                        for e in evidence
                    ],
                }
            )

    @tool
    def get_claim_impact(claim_id: int) -> str:
        """Which plan values a decision drove - the reverse of the trace. Rows carry
        ``plan_value_id`` to cite (ref kind "plan_value")."""
        with session_factory() as s:
            try:
                claim = s.get(Claim, int(claim_id))
            except Exception as e:  # noqa: BLE001 - missing brain tables in a finance-only DB
                return _claims_unavailable(s, e)
            if claim is None:
                return _err(f"claim {claim_id} not found")
            cats = _categories(s)
            rows = s.scalars(select(PlanValue).where(PlanValue.claim_id == claim.id)).all()
            out = []
            for pv in rows:
                scen = s.get(Scenario, pv.scenario_id)
                out.append(
                    {
                        "plan_value_id": pv.id,
                        "scenario_kind": scen.kind.value if scen is not None else None,
                        "category_code": cats[pv.category_id].code,
                        "year": pv.year,
                        "value": pv.value,
                        "path": pv.path.value,
                    }
                )
        return _dumps({"rows": out})

    @tool
    def get_external_notes() -> str:
        """All external notes (manual and AI-scanned): id, category, year, text, author, source."""
        with session_factory() as s:
            cats = _categories(s)
            rows = [_note_dict(n, cats) for n in s.scalars(select(ExternalNote).order_by(ExternalNote.id)).all()]
        return _dumps({"rows": rows})

    @tool
    def get_env_framework() -> str:
        """The environmental scan framework: domains and their positions with the categories they affect."""
        # Pretty-printed (one field per line): the 54-position framework is the largest tool
        # result and may be evicted to /large_tool_results/ by the context middleware; the
        # model then pages through it with read_file(offset, limit), which is line-based.
        return json.dumps(load_env_framework(), ensure_ascii=False, indent=1, default=str)

    @tool
    def get_control_table() -> str:
        """Control table: rules a revenue proposal must satisfy plus global planning settings."""
        return _dumps(load_control_table())

    @tool
    def get_plan_vs_actual(scenario_kind: str, year: int) -> str:
        """Plan vs actual per category for one scenario and year: plan, actual, deviation
        (actual - plan), deviation %, alpha/beta/R^2, and for costs the revenue-driven part
        (beta * revenue deviation) and the residual. Pure arithmetic over the tables; the
        deviation explanation is checked against exactly these figures."""
        with session_factory() as s:
            return _dumps(plan_vs_actual(s, scenario_kind, year))

    return [
        get_actuals,
        get_parameters,
        get_plan_values,
        get_plan_value,
        get_trace,
        get_external_notes,
        get_env_framework,
        get_control_table,
        get_plan_vs_actual,
        get_backtest_summary,
        get_decisions,
        get_decision,
        get_claim_impact,
    ]


# --------------------------------------------------------------------------- write tools


def validate_revenue_proposal(
    session: Session,
    ctx: AiRunContext,
    *,
    year: int,
    proposed_value: float,
    rationale: str,
    cited_note_ids: list[int],
) -> str | None:
    """Return an error message when the proposal breaks a rule, else None.

    Rules: non-empty rationale (hard rule 2), year matches the run, value within
    ``max_deviation_from_default_pct`` of ``ctx.default_value``, and when
    ``must_cite_note`` at least one cited id that exists in ``external_note``.
    """
    if not rationale or not rationale.strip():
        return "rationale is required: every AI value must carry a written rationale"
    if ctx.year is not None and int(year) != int(ctx.year):
        return f"year {year} does not match the target year of this run ({ctx.year})"
    if proposed_value is None or proposed_value <= 0:
        return "proposed_value must be a positive number (k EUR)"
    rules = load_control_table().get("revenue_proposal", {})
    max_pct = rules.get("max_deviation_from_default_pct")
    if max_pct is not None:
        if ctx.default_value is None:
            return "no valorized default available in this run; cannot check the deviation bound"
        dev_pct = abs(proposed_value - ctx.default_value) / ctx.default_value * 100.0
        if dev_pct > float(max_pct) + 1e-9:
            return (
                f"proposed_value {proposed_value:,.1f} deviates {dev_pct:.1f}% from the default "
                f"{ctx.default_value:,.1f}; the control table allows at most {max_pct}%"
            )
    if rules.get("must_cite_note"):
        ids = [int(i) for i in (cited_note_ids or [])]
        if not ids:
            return "the control table requires at least one cited external_note id"
        existing = set(session.scalars(select(ExternalNote.id).where(ExternalNote.id.in_(ids))).all())
        missing = sorted(set(ids) - existing)
        if missing:
            return f"cited note ids do not exist: {missing}"
    return None


def record_proposal(
    session: Session,
    ctx: AiRunContext,
    *,
    year: int,
    proposed_value: float,
    rationale: str,
    cited_note_ids: list[int],
    response_text: str | None = None,
) -> AiRecord | str:
    """Validate and insert (or update, within the same run) the proposal's ai_record.

    Returns the record, or the error string when rejected (nothing written).
    """
    err = validate_revenue_proposal(
        session, ctx, year=year, proposed_value=proposed_value, rationale=rationale, cited_note_ids=cited_note_ids
    )
    if err:
        return err
    rev = _category_by_code(session, "REV")
    payload = {
        "year": int(year),
        "proposed_value": float(proposed_value),
        "rationale": rationale,
        "cited_note_ids": [int(i) for i in cited_note_ids],
        "default_value": ctx.default_value,
    }
    rec = session.get(AiRecord, ctx.ai_record_id) if ctx.ai_record_id else None
    if rec is None:
        rec = AiRecord(
            touchpoint=Touchpoint.revenue_proposal,
            prompt_text=ctx.prompt_text or "(prompt not rendered)",
            response_text=response_text or _dumps(payload),
            rationale=rationale,
            model_version=ctx.model_version,
            proposed_value=float(proposed_value),
            scenario_id=ctx.scenario_id,
            category_id=rev.id if rev else None,
            year=int(year),
            status=AiStatus.proposed,
        )
        session.add(rec)
    else:
        rec.response_text = response_text or _dumps(payload)
        rec.rationale = rationale
        rec.proposed_value = float(proposed_value)
        rec.year = int(year)
    session.commit()
    ctx.ai_record_id = rec.id
    return rec


def make_write_tools(session_factory: SessionFactory, ai_record_id_holder: AiRunContext) -> list[BaseTool]:
    """The only two tools that write: an ai_scan external note and the revenue proposal."""
    ctx = ai_record_id_holder

    @tool
    def record_external_note(
        text: str,
        domain: str,
        position: str,
        category_code: str | None = None,
        year: int | None = None,
    ) -> str:
        """Record one material environmental finding as an external note (source=ai_scan).
        text: self-contained finding (what, when, magnitude if known, source). domain/position:
        the framework ids or names (e.g. "D2 Economic", "D2.P2 Inflation"). category_code:
        REV/MAT/EXT/PERS/OTH if the finding hits one category. year: the plan year it hits."""
        if not text or not text.strip():
            return _err("text is required: a note without content is not recorded")
        with session_factory() as s:
            cat_id = None
            if category_code:
                cat = _category_by_code(s, category_code)
                if cat is None:
                    return _err(f"unknown category code {category_code!r}")
                cat_id = cat.id
            note = ExternalNote(
                category_id=cat_id,
                year=int(year) if year is not None else None,
                text=f"[{domain} / {position}] {text.strip()}",
                author=f"ai:{ctx.model_version}",
                source=NoteSource.ai_scan,
                ai_record_id=ctx.ai_record_id,
            )
            s.add(note)
            s.commit()
            ctx.note_ids.append(note.id)
            return _dumps({"note_id": note.id, "source": "ai_scan", "status": "recorded"})

    @tool
    def record_revenue_proposal(year: int, proposed_value: float, rationale: str, cited_note_ids: list[int]) -> str:
        """Record the revenue proposal for the target year as an ai_record with status=proposed.
        Validated against the control table: the value must stay within the allowed deviation
        from the valorized default, the rationale must be non-empty, and at least one existing
        external_note id must be cited. Returns an error to fix if a rule is broken."""
        with session_factory() as s:
            out = record_proposal(
                s,
                ctx,
                year=year,
                proposed_value=proposed_value,
                rationale=rationale,
                cited_note_ids=list(cited_note_ids or []),
            )
            if isinstance(out, str):
                return _err(out)
            return _dumps(
                {
                    "ai_record_id": out.id,
                    "status": out.status.value,
                    "year": out.year,
                    "proposed_value": out.proposed_value,
                    "default_value": ctx.default_value,
                    "note": "proposal recorded; it enters the plan only after human confirmation",
                }
            )

    return [record_external_note, record_revenue_proposal]
