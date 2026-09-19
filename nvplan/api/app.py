"""FastAPI routes (PDF section 4: plan grid, click-through derivation, scenario switch,
prompt viewer, backtest report - exposed as JSON for a UI to render).

``create_app(db_url=None, session_factory=None)`` builds the app; the session
factory lives on ``app.state.session_factory``. AI routes take their chat model
from ``app.state.model_factory`` (``callable(touchpoint, context) -> BaseChatModel``;
tests inject scripted fakes). When it is ``None`` the real model
(``nvplan.ai.get_model`` -> ``ChatAnthropic(config.AI_MODEL)``) is used, which needs an
Anthropic credential (``ANTHROPIC_API_KEY`` in ``.env`` or the environment); without either the
AI routes answer 503 with ``nvplan.ai.credential_hint()`` as the detail.

Error mapping: LookupError -> 404, GateError -> 409,
ProposalRejected / ExplanationRejected / AnswerRejected -> 422,
ValueError -> 400, MissingCredentials -> 503, ModelRefused -> 502.
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path
from typing import Any, Callable, Iterator

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session, sessionmaker

from nvplan import config
from nvplan.ai import (
    AnswerRejected,
    ExplanationRejected,
    MissingCredentials,
    ModelRefused,
    ProposalRejected,
    ask,
    audit,
    credential_hint,
    credentials_available,
    run_deviation_explanation,
    run_env_scan,
    run_revenue_proposal,
)
from nvplan.api import queries as q
from nvplan.api import schemas as S
from nvplan.db.models import AiRecord
from nvplan.db.session import get_engine, init_db, seed_categories
from nvplan.ingest.actuals import ingest_actuals, load_actuals_csv, load_actuals_xlsx
from nvplan.services.gate import GateError, confirm_proposal, reject_proposal
from nvplan.services.planning import PlanRun, run_plan
from nvplan.services.trace import render_trace, trace_plan_value, trace_statement_line

# The brain / bridge endpoints (PLATFORM.md §7, §7.1; UI.md Part 1). ``nvplan/api`` is the
# platform's demo/composition layer - the one place meant to import both ``brainkit`` and
# ``bridge`` (see ``nvplan/api/queries.py``'s import comment for the layering note this
# supersedes here, by the same UI.md instruction).
from brainkit.indexer import reindex_tree
from brainkit.validate import Finding, validate_tree
from bridge.db import init_platform_db
from bridge.effects import decided_effects, revenue_override
from bridge.lookup import make_derivation_lookup

ModelFactory = Callable[[str, dict[str, Any]], Any]

XLSX_TYPES = ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/vnd.ms-excel")

# repo root: nvplan/api/app.py -> nvplan/api -> nvplan -> repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BRAIN_ROOT = REPO_ROOT / "brain"
STATIC_DIR = Path(__file__).resolve().parent / "static"


# --------------------------------------------------------------------------- factory


def make_session_factory(db_url: str | None = None) -> sessionmaker:
    """Engine + bound sessionmaker. SQLite gets ``check_same_thread=False`` because the AI
    tools run in worker threads and share the factory.

    ``bridge.db.init_platform_db`` creates both the nvplan tables and the provenance
    (claim/evidence/claim_link) tables on the same engine, so one session reads both
    (PLATFORM.md §7.1). ``create_all`` only creates tables that don't already exist, so an
    existing finance-only database file keeps serving every route it already served and
    additionally gains the brain tables on next boot - no migration step, no error."""
    url = db_url or os.environ.get("NVPLAN_DB_URL") or config.DEFAULT_DB_URL
    kwargs = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
    engine = init_platform_db(init_db(get_engine(url, **kwargs)))
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        seed_categories(s)
    return factory


def create_app(db_url: str | None = None, session_factory: Callable[[], Session] | None = None) -> FastAPI:
    app = FastAPI(
        title="NewVision planning PoC",
        version="0.0.1",
        description="Deterministic planning engine with traceable derivations and three advisory AI touchpoints. "
                    f"Figures labelled {config.ILLUSTRATIVE_LABEL} are generated dummy data.",
    )
    app.state.session_factory = session_factory or make_session_factory(db_url)
    app.state.db_url = db_url or os.environ.get("NVPLAN_DB_URL") or config.DEFAULT_DB_URL
    app.state.model_factory = None  # type: ModelFactory | None
    _register(app)
    return app


def get_session(request: Request) -> Iterator[Session]:
    session: Session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


def _plan_run_out(run: PlanRun) -> dict[str, Any]:
    return {
        "scenario_ids": run.scenario_ids, "parameter_ids": run.parameter_ids, "n_derivations": run.n_derivations,
        "n_plan_values": run.n_plan_values, "n_statement_lines": run.n_statement_lines, "label": run.label,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


def _repo_relative(path: Path) -> str:
    """``path``, relative to the repository root when possible - never absolute (UI.md Part
    1). Falls back to the path as given for anything outside the repo (a custom
    ``brain_root`` elsewhere on disk), rather than raising."""
    try:
        return str(Path(path).resolve().relative_to(REPO_ROOT).as_posix())
    except ValueError:
        return str(path)


def _finding_dict(f: Finding) -> dict[str, Any]:
    return {"path": _repo_relative(f.path), "line": f.line, "code": f.code, "message": f.message,
            "severity": f.severity}


def resolve_model(app: FastAPI, touchpoint: str, context: dict[str, Any]):
    """Injected model factory first; else the real model if a credential is resolvable; else 503.

    The 503 detail is ``nvplan.ai.credential_hint()`` verbatim - the same message
    ``MissingCredentials`` and ``nvplan-ai-check`` print, so there is one wording of "what to do
    about a missing key" in the whole project. (Tests inject ``app.state.model_factory``
    instead; that path never touches a credential.)"""
    factory: ModelFactory | None = app.state.model_factory
    if factory is not None:
        return factory(touchpoint, context)
    if not credentials_available():
        raise HTTPException(status_code=503, detail=credential_hint())
    from nvplan.ai import get_model

    return get_model()


# --------------------------------------------------------------------------- routes


def _register(app: FastAPI) -> None:
    @app.exception_handler(LookupError)
    async def _not_found(_r, exc):  # noqa: ANN001
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(GateError)
    async def _gate(_r, exc):  # noqa: ANN001
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ProposalRejected)
    async def _rejected(_r, exc):  # noqa: ANN001
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(ExplanationRejected)
    async def _explanation_rejected(_r, exc):  # noqa: ANN001
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(AnswerRejected)
    async def _answer_rejected(_r, exc):  # noqa: ANN001
        # UI.md Part 3: an unsourced figure discards the whole answer; nothing was persisted.
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(MissingCredentials)
    async def _no_credentials(_r, exc):  # noqa: ANN001
        # get_model() raised inside a run (no factory injected, credential vanished mid-flight).
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(ModelRefused)
    async def _refused(_r, exc):  # noqa: ANN001
        # HTTP 200 + stop_reason="refusal" from the provider: the upstream model declined and
        # nothing was persisted -> 502, not a 5xx of ours and not a client error.
        return JSONResponse(
            status_code=502,
            content={"detail": f"the model declined this request; no ai_record was written. {exc}"},
        )

    @app.exception_handler(ValueError)
    async def _bad_request(_r, exc):  # noqa: ANN001
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    # ---- health ----------------------------------------------------------------

    @app.get("/health", response_model=S.HealthOut)
    def health(request: Request, session: Session = Depends(get_session)):
        counts = q.table_counts(session)
        return {"status": "ok", "db_url": request.app.state.db_url, "illustrative": q.illustrative_flag(session),
                "n_actuals": counts["actual"], "n_scenarios": counts["scenario"]}

    # ---- ingest ----------------------------------------------------------------

    @app.post("/ingest", response_model=S.IngestOut)
    async def ingest(
        request: Request,
        source_label: str | None = Query(None, description="Label for rows without one (required for uploads)"),
        path: str | None = Query(None, description="Server-side CSV/XLSX path; default: the illustrative CSV"),
        with_notes: bool = Query(True, description="Also seed the illustrative external notes (once)"),
        session: Session = Depends(get_session),
    ):
        """Load actuals. Send the file as the raw request body (Content-Type text/csv or the xlsx
        type) or point ``path`` at a file; with neither, ``data/illustrative/actuals.csv`` is loaded."""
        body = await request.body()
        ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if body:
            if ctype in XLSX_TYPES or (ctype == "application/octet-stream" and body[:2] == b"PK"):
                df, source = load_actuals_xlsx(io.BytesIO(body), source_label=source_label), "upload.xlsx"
            else:
                df, source = load_actuals_csv(io.StringIO(body.decode("utf-8-sig")), source_label=source_label), "upload.csv"
        else:
            p = Path(path) if path else config.DATA_DIR / "actuals.csv"
            if not p.exists():
                raise LookupError(f"file not found: {p}")
            df = load_actuals_xlsx(p, source_label=source_label) if p.suffix.lower() in (".xlsx", ".xlsm") \
                else load_actuals_csv(p, source_label=source_label)
            source = str(p)
        n = ingest_actuals(session, df, source_label=source_label)
        labels = sorted(set(df["source_label"].astype(str)))
        notes = q.seed_external_notes(session) if with_notes else 0
        return {"rows": n, "source_label": ", ".join(labels), "source": source,
                "illustrative": q.illustrative_flag(session), "notes_seeded": notes}

    # ---- plan ------------------------------------------------------------------

    @app.post("/plan/run", response_model=S.PlanRunOut)
    def plan_run(body: S.PlanRunIn, session: Session = Depends(get_session)):
        run = run_plan(session, created_by=body.created_by, label_suffix=body.label_suffix)
        return _plan_run_out(run)

    @app.get("/plan/grid", response_model=S.PlanGridOut)
    def plan_grid(scenario_kind: str = "base", scenario_id: int | None = None, session: Session = Depends(get_session)):
        return q.plan_grid(session, scenario_kind, scenario_id)

    @app.get("/plan/scenarios", response_model=list[S.ScenarioOut])
    def plan_scenarios(session: Session = Depends(get_session)):
        return q.list_scenarios(session)

    # ---- statements ------------------------------------------------------------

    @app.get("/statements/{scenario_kind}", response_model=S.StatementGridOut)
    def statements(scenario_kind: str, statement: str = Query("pl", pattern="^(pl|bs|cf)$"),
                   scenario_id: int | None = None, session: Session = Depends(get_session)):
        return q.statement_grid(session, scenario_kind, statement, scenario_id)

    # ---- trace -----------------------------------------------------------------

    @app.get("/trace/plan-value/{plan_value_id}", response_model=None)
    def trace_pv(plan_value_id: int, format: str = Query("json", pattern="^(json|text)$"),
                 max_depth: int = 12, session: Session = Depends(get_session)):
        tree = trace_plan_value(session, plan_value_id, max_depth=max_depth)
        if format == "text":
            return PlainTextResponse(render_trace(tree))
        return tree.to_dict()

    @app.get("/trace/statement-line/{statement_line_id}", response_model=None)
    def trace_sl(statement_line_id: int, format: str = Query("json", pattern="^(json|text)$"),
                 max_depth: int = 20, session: Session = Depends(get_session)):
        tree = trace_statement_line(session, statement_line_id, max_depth=max_depth)
        if format == "text":
            return PlainTextResponse(render_trace(tree))
        return tree.to_dict()

    # ---- ai records (prompt viewer) + gate --------------------------------------

    @app.get("/ai/records", response_model=list[S.AiRecordOut])
    def ai_records(touchpoint: str | None = None, status: str | None = None, session: Session = Depends(get_session)):
        return [q.ai_record_dict(session, r) for r in q.list_ai_records(session, touchpoint, status)]

    @app.get("/ai/records/{record_id}", response_model=S.AiRecordDetailOut)
    def ai_record(record_id: int, session: Session = Depends(get_session)):
        rec = session.get(AiRecord, record_id)
        if rec is None:
            raise LookupError(f"ai_record {record_id} not found")
        # The per-call audit log (nvplan.ai.audit) is only on the detail route; the list stays light.
        # total_usage is recomputed from the persisted log, so records written before it existed work too.
        call_log = list(rec.call_log_json or [])
        return {**q.ai_record_dict(session, rec), "call_log": call_log,
                "total_usage": audit.total_usage(call_log) if rec.call_log_json is not None else None}

    @app.post("/ai/records/{record_id}/confirm", response_model=S.ConfirmOut)
    def ai_confirm(record_id: int, body: S.ConfirmIn, session: Session = Depends(get_session)):
        run = confirm_proposal(session, record_id, confirmed_by=body.confirmed_by)
        rec = session.get(AiRecord, record_id)
        return {"record": q.ai_record_dict(session, rec), "run": _plan_run_out(run) if run else None}

    @app.post("/ai/records/{record_id}/reject", response_model=S.AiRecordOut)
    def ai_reject(record_id: int, body: S.RejectIn, session: Session = Depends(get_session)):
        rec = reject_proposal(session, record_id, rejected_by=body.rejected_by)
        return q.ai_record_dict(session, rec)

    # ---- ai touchpoints ----------------------------------------------------------

    @app.post("/ai/env-scan", response_model=S.AiRecordOut)
    def ai_env_scan(body: S.EnvScanIn, request: Request):
        factory = request.app.state.session_factory
        model = resolve_model(request.app, "env_scan", {"positions_subset": body.positions_subset})
        rec = run_env_scan(factory, model=model, positions_subset=body.positions_subset)
        with factory() as s:
            return q.ai_record_dict(s, s.get(AiRecord, rec.id))

    @app.post("/ai/revenue-proposal", response_model=S.RevenueProposalOut)
    def ai_revenue_proposal(body: S.RevenueProposalIn, request: Request):
        factory = request.app.state.session_factory
        with factory() as s:
            default = q.default_revenue(s, body.scenario_kind, body.year)
            ctx = {"scenario_kind": body.scenario_kind, "year": body.year, "default_value": default,
                   "note_ids": q.note_ids(s)}
        model = resolve_model(request.app, "revenue_proposal", ctx)
        rec = run_revenue_proposal(factory, scenario_kind=body.scenario_kind, year=body.year,
                                   default_value=default, model=model)
        with factory() as s:
            return {"record": q.ai_record_dict(s, s.get(AiRecord, rec.id)), "default_value": default}

    @app.post("/ai/deviation-explanation", response_model=S.AiRecordOut)
    def ai_deviation(body: S.DeviationExplanationIn, request: Request):
        factory = request.app.state.session_factory
        with factory() as s:
            q.latest_scenario(s, body.scenario_kind)  # 404 if there is no plan
        model = resolve_model(request.app, "deviation_explanation",
                              {"scenario_kind": body.scenario_kind, "year": body.year})
        rec = run_deviation_explanation(factory, scenario_kind=body.scenario_kind, year=body.year, model=model)
        with factory() as s:
            return q.ai_record_dict(s, s.get(AiRecord, rec.id))

    # ---- conversational assistant (UI.md Part 3) --------------------------------

    @app.post("/assistant/ask", response_model=None)
    def assistant_ask(body: S.AssistantAskIn, request: Request):
        factory = request.app.state.session_factory
        model = resolve_model(
            request.app, "assistant", {"question": body.question, "scenario_kind": body.scenario_kind}
        )
        answer = ask(factory, body.question, scenario_kind=body.scenario_kind, model=model)
        return answer.model_dump()

    # ---- analysis --------------------------------------------------------------

    @app.get("/backtest", response_model=S.BacktestOut)
    def backtest(window_len: int = Query(5, ge=3), session: Session = Depends(get_session)):
        return q.backtest_report(session, window_len=window_len)

    @app.get("/deviation", response_model=S.DeviationOut)
    def deviation(scenario_kind: str = "base", year: int | None = None, scenario_id: int | None = None,
                  session: Session = Depends(get_session)):
        return q.deviation(session, scenario_kind, year, scenario_id)

    @app.get("/deviation/backtest-year", response_model=S.BacktestYearOut)
    def deviation_backtest_year(year: int = Query(..., description="a year with actuals, e.g. 2025"),
                                window_len: int = Query(5, ge=3), session: Session = Depends(get_session)):
        return q.backtest_year_deviation(session, year, window_len=window_len)

    # ---- brain / bridge (PLATFORM.md §7, §7.1; UI.md Part 1) --------------------

    @app.post("/brain/reindex", response_model=S.BrainReindexOut)
    def brain_reindex(
        brain_root: str | None = Query(None, description="default: the repository's brain/ directory"),
        session: Session = Depends(get_session),
    ):
        root = Path(brain_root) if brain_root else DEFAULT_BRAIN_ROOT
        lookup = make_derivation_lookup(session)
        report = reindex_tree(session, root, strict=True, derivation_lookup=lookup)
        return {
            "files_seen": report.files_seen,
            "indexed": report.indexed,
            "skipped_unchanged": report.skipped_unchanged,
            "rejected": [_repo_relative(p) for p in report.rejected],
            "findings": [_finding_dict(f) for f in report.findings],
        }

    @app.get("/brain/validate", response_model=S.BrainValidateOut)
    def brain_validate(brain_root: str | None = Query(None, description="default: the repository's brain/ directory")):
        root = Path(brain_root) if brain_root else DEFAULT_BRAIN_ROOT
        findings = validate_tree(root)
        errors = [_finding_dict(f) for f in findings if f.severity == "error"]
        warnings = [_finding_dict(f) for f in findings if f.severity == "warning"]
        return {"errors": errors, "warnings": warnings, "clean": not errors}

    @app.get("/brain/claims", response_model=list[S.ClaimOut])
    def brain_claims(kind: str | None = None, status: str | None = None, session: Session = Depends(get_session)):
        return q.list_claims(session, kind=kind, status=status)

    @app.get("/brain/claims/{claim_id}", response_model=S.ClaimDetailOut)
    def brain_claim_detail(claim_id: int, session: Session = Depends(get_session)):
        return q.claim_detail(session, claim_id)

    @app.get("/brain/claims/{claim_id}/impact", response_model=list[S.ImpactRowOut])
    def brain_claim_impact(claim_id: int, session: Session = Depends(get_session)):
        return q.claim_impact(session, claim_id)

    @app.get("/brain/effects", response_model=list[S.DecidedEffectOut])
    def brain_effects(session: Session = Depends(get_session)):
        return q.decided_effects_list(session)

    @app.post("/bridge/apply", response_model=S.BridgeApplyOut)
    def bridge_apply(body: S.BridgeApplyIn = S.BridgeApplyIn(), session: Session = Depends(get_session)):
        effects = decided_effects(session)
        plan = revenue_override(effects)
        if not plan.overrides:
            return {
                "plan_run": None, "applied": [], "shadowed": list(plan.shadowed),
                "message": "no decided effect to apply; the plan was not rerun",
            }
        run = run_plan(
            session, created_by=body.created_by, revenue_override=plan.overrides, label_suffix=body.label_suffix
        )
        return {
            "plan_run": _plan_run_out(run), "applied": sorted(plan.overrides), "shadowed": list(plan.shadowed),
            "message": None,
        }

    # ---- static UI (UI.md Part 2) -----------------------------------------------

    @app.get("/", include_in_schema=False)
    def index():
        index_path = STATIC_DIR / "index.html"
        if not index_path.is_file():
            return PlainTextResponse("nvplan/api/static/index.html not found", status_code=404)
        return FileResponse(str(index_path))

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# --------------------------------------------------------------------------- serve


def serve(argv: list[str] | None = None) -> None:
    """``nvplan-serve [--db sqlite:///nvplan.db] [--host 127.0.0.1] [--port 8000]``."""
    import argparse

    import uvicorn

    p = argparse.ArgumentParser(prog="nvplan-serve", description="Run the NewVision planning API.")
    p.add_argument("--db", default=None, help=f"SQLAlchemy URL (default $NVPLAN_DB_URL or {config.DEFAULT_DB_URL})")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args(sys.argv[1:] if argv is None else argv)
    uvicorn.run(create_app(args.db), host=args.host, port=args.port)
