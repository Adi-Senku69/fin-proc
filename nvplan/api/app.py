"""FastAPI routes (PDF section 4: plan grid, click-through derivation, scenario switch,
prompt viewer, backtest report - exposed as JSON for a UI to render).

``create_app(db_url=None, session_factory=None)`` builds the app; the session
factory lives on ``app.state.session_factory``. AI routes take their chat model
from ``app.state.model_factory`` (``callable(touchpoint, context) -> BaseChatModel``;
tests inject scripted fakes). When it is ``None`` the real model
(``nvplan.ai.get_model`` -> ``ChatAnthropic(config.AI_MODEL)``) is used, which needs
``ANTHROPIC_API_KEY``; without either the AI routes answer 503.

Error mapping: LookupError -> 404, GateError -> 409, ProposalRejected -> 422,
ValueError -> 400.
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path
from typing import Any, Callable, Iterator

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.orm import Session, sessionmaker

from nvplan import config
from nvplan.ai import ProposalRejected, run_deviation_explanation, run_env_scan, run_revenue_proposal
from nvplan.api import queries as q
from nvplan.api import schemas as S
from nvplan.db.models import AiRecord
from nvplan.db.session import get_engine, init_db, seed_categories
from nvplan.ingest.actuals import ingest_actuals, load_actuals_csv, load_actuals_xlsx
from nvplan.services.gate import GateError, confirm_proposal, reject_proposal
from nvplan.services.planning import PlanRun, run_plan
from nvplan.services.trace import render_trace, trace_plan_value, trace_statement_line

ModelFactory = Callable[[str, dict[str, Any]], Any]

XLSX_TYPES = ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/vnd.ms-excel")


# --------------------------------------------------------------------------- factory


def make_session_factory(db_url: str | None = None) -> sessionmaker:
    """Engine + bound sessionmaker. SQLite gets ``check_same_thread=False`` because the AI
    tools run in worker threads and share the factory."""
    url = db_url or os.environ.get("NVPLAN_DB_URL") or config.DEFAULT_DB_URL
    kwargs = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
    engine = init_db(get_engine(url, **kwargs))
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


def resolve_model(app: FastAPI, touchpoint: str, context: dict[str, Any]):
    """Injected model factory first; else the real model if a key is present; else 503."""
    factory: ModelFactory | None = app.state.model_factory
    if factory is not None:
        return factory(touchpoint, context)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(
            status_code=503,
            detail=(f"AI touchpoint '{touchpoint}' needs a model: set ANTHROPIC_API_KEY (model {config.AI_MODEL}) "
                    "or inject app.state.model_factory. The deterministic plan does not depend on it."),
        )
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
        return {**q.ai_record_dict(session, rec), "call_log": list(rec.call_log_json or [])}

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
