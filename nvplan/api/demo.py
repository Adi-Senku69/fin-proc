"""The story told live (PDF week 3): ingest -> plan -> trace -> backtest -> AI proposal ->
human confirmation -> rerun -> statements -> deviation explanation -> row counts.

``main(db_path="nvplan_demo.db")``: a fresh SQLite file every run.
AI: the scripted fakes of :mod:`nvplan.ai.fake` **by default** - offline, free, ~1 s - which is
stated loudly in the output. The real model (``config.AI_MODEL`` via ``ANTHROPIC_API_KEY``) is
only used when the caller explicitly opts in with ``--live`` / ``run_demo(..., live=True)``, and
then the banner says that real tokens are being spent. A present credential alone never makes the
demo go live: ``nvplan.config`` loads ``.env`` at import, so that would be a silent bill.
``--live`` without a resolvable credential fails fast with the credential hint (exit code 2)
instead of quietly falling back to the fakes. Everything else is deterministic.

The deviation explanation needs a plan for a year that also has actuals. The live
plan covers 2026-2030 (no actuals yet), so step 7 persists one extra plan run that
fits on 2020-2024 and plans 2025-2029 - the backtest year - with an opening balance
sheet derived for end-2024 (``core.statements.derive_opening``). It is labelled as
such; it becomes the latest ``base`` scenario of the demo DB, which the API takes
by ``scenario_id`` when you want the live one.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import yaml
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from nvplan import config
from nvplan.ai import run_deviation_explanation, run_env_scan, run_revenue_proposal
from nvplan.ai.agents import MissingCredentials, credential_hint, credentials_available
from nvplan.ai.fake import scripted_deviation_model, scripted_env_scan_model, scripted_revenue_model
from nvplan.ai.tools import plan_vs_actual
from nvplan.api import queries as q
from nvplan.core.statements import derive_opening, load_mapping
from nvplan.db.models import AiRecord, Category, Actual
from nvplan.db.session import get_engine, init_db, seed_categories
from nvplan.ingest.actuals import ingest_actuals, load_actuals_csv
from nvplan.services.gate import confirm_proposal
from nvplan.services.planning import PlanRun, run_plan
from nvplan.services.trace import find_plan_value, render_trace, trace_plan_value

BACKTEST_YEAR = 2025
PROPOSAL_YEAR = 2027
CONFIRMER = "C. Andres"
RULE = "=" * 78


# --------------------------------------------------------------------------- formatting


def _table(headers: list[str], rows: list[list[Any]], out: Callable[[str], None]) -> None:
    def fmt(v: Any) -> str:
        if isinstance(v, float):
            return f"{v:,.1f}" if abs(v) >= 100 else f"{v:.4f}"
        return str(v)

    cells = [[fmt(v) for v in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in cells)) for i, h in enumerate(headers)] if cells else [len(h) for h in headers]
    line = "  ".join(f"{h:<{w}}" if i == 0 else f"{h:>{w}}" for i, (h, w) in enumerate(zip(headers, widths)))
    out(line)
    out("  ".join("-" * w for w in widths))
    for r in cells:
        out("  ".join(f"{c:<{w}}" if i == 0 else f"{c:>{w}}" for i, (c, w) in enumerate(zip(r, widths))))


def _section(out: Callable[[str], None], n: int, title: str) -> None:
    out("")
    out(RULE)
    out(f"{n}. {title}")
    out(RULE)


def _print_grid(grid: dict[str, Any], out: Callable[[str], None], codes: list[str] | None = None) -> None:
    years = grid["years"]
    rows = []
    for r in grid["rows"]:
        if codes and r["category_code"] not in codes:
            continue
        cells = r["cells"]
        paths = {cells[y]["path"] for y in years if y in cells}
        rows.append([f"{r['category_code']:<5}{r['name'][:28]}", *[cells[y]["value"] if y in cells else "" for y in years],
                     "/".join(sorted(paths))])
    _table(["category", *[str(y) for y in years], "path"], rows, out)


def _print_parameters(grid: dict[str, Any], out: Callable[[str], None]) -> None:
    rows = []
    for p in grid["parameters"]:
        if p["category_code"] == "REV":
            rows.append(["REV (growth g)", "", "", "", p.get("growth_rate"), f"{p.get('window_from')}-{p.get('window_to')}"])
        else:
            rows.append([p["category_code"], p.get("alpha"), p.get("beta"), p.get("r_squared"), p.get("valorization_rate"),
                         f"{p.get('window_from')}-{p.get('window_to')}"])
    _table(["category", "alpha", "beta", "R2", "v / g", "window"], rows, out)


def _excerpt(text: str, n_lines: int = 8) -> str:
    lines = text.splitlines()
    head = lines[:n_lines]
    more = f"  ... ({len(lines) - n_lines} more lines; full prompt in ai_record.prompt_text)" if len(lines) > n_lines else ""
    return "\n".join(f"  | {ln}" for ln in head) + ("\n" + more if more else "")


# --------------------------------------------------------------------------- backtest-year plan


def backtest_year_mapping(session, year: int, base_mapping: str | Path | None = None) -> Path:
    """Write a temp copy of bs_mapping.yaml whose opening balance sheet is derived for end of
    ``year - 1`` from the actual P&L of ``year`` (steady-state proxy; cash as in the yaml)."""
    mapping = load_mapping(base_mapping or config.DATA_DIR / "bs_mapping.yaml")
    cats = {c.id: c.code for c in session.scalars(select(Category)).all()}
    acts = {cats[a.category_id]: a.value for a in session.scalars(select(Actual).where(Actual.year == year)).all()}
    pl = pd.DataFrame([{"line_code": ln, "year": year, "value": acts[code]}
                       for ln, code in (("revenue", "REV"), ("material", "MAT"), ("external", "EXT"))])
    inv = pd.read_csv(config.DATA_DIR / "investment_plan.csv")
    cash = float(mapping["opening_balance_sheet"].get("cash", 0.0))
    opening = derive_opening(pl, inv, mapping, cash=cash)
    opening["note"] = f"derived for the backtest year {year} (opening = end {year - 1}); DEMO ONLY"
    mapping["opening_balance_sheet"] = opening
    fh = tempfile.NamedTemporaryFile("w", suffix=".yaml", prefix="bs_mapping_bt_", delete=False, encoding="utf-8")
    with fh:
        yaml.safe_dump(mapping, fh, sort_keys=False)
    return Path(fh.name)


def run_backtest_year_plan(session, year: int, created_by: str, window_len: int = 5) -> PlanRun:
    """Persist a plan fitted on the window ending ``year-1`` with plan years starting at ``year``."""
    mapping_path = backtest_year_mapping(session, year)
    try:
        return run_plan(
            session, window=(year - window_len, year - 1), plan_years=(year, year + 4), created_by=created_by,
            label_suffix=f" (backtest fit {year - window_len}-{year - 1} -> {year}; for the deviation explanation)",
            mapping_path=mapping_path,
        )
    finally:
        mapping_path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- main


def main(db_path: str | None = None, use_fake_ai: bool | None = None, live: bool = False,
         out: Callable[[str], None] = print) -> int:
    """Console entry point (``nvplan-demo [--db FILE] [--live]``); 0 on success, 2 without a credential.

    Called with ``db_path`` it skips argv parsing (tests). The scripted fakes are the default; only
    ``--live`` / ``live=True`` uses the real model, and then a missing credential is a fast, loud
    failure (exit code 2) rather than a silent fallback."""
    if db_path is None:  # console script: parse argv
        p = argparse.ArgumentParser(prog="nvplan-demo", description="End-to-end NewVision planning demo.")
        p.add_argument("--db", default="nvplan_demo.db", help="SQLite file (recreated)")
        p.add_argument("--live", action="store_true",
                       help=f"opt into the real model ({config.AI_MODEL}): SPENDS REAL TOKENS and takes minutes; "
                            "without a credential it fails instead of falling back")
        p.add_argument("--fake-ai", action="store_true",
                       help="accepted no-op alias: the scripted fake models are the default now")
        args = p.parse_args(sys.argv[1:])
        db_path, live = args.db, (live or args.live)
        if args.fake_ai:
            use_fake_ai = True
    try:
        run_demo(db_path, use_fake_ai=use_fake_ai, live=live, out=out)
    except MissingCredentials as exc:
        out(f"!! --live was requested but no Anthropic credential is available. {exc}")
        return 2
    return 0


def run_demo(db_path: str, use_fake_ai: bool | None = None, live: bool = False,
             out: Callable[[str], None] = print) -> dict[str, int]:
    """Run the whole story on a fresh SQLite file; returns the row counts per table.

    Fakes unless ``live=True``; ``use_fake_ai=True`` forces the fakes even then (``use_fake_ai=False``
    is *not* an opt-in to the live model). ``live=True`` without a credential raises
    :class:`nvplan.ai.agents.MissingCredentials` before any work is done."""
    t_start = time.perf_counter()
    fake = True if use_fake_ai is True else (not live)
    if not fake and not credentials_available():
        raise MissingCredentials(credential_hint())

    out(RULE)
    out("NewVision AI-supported planning PoC - end-to-end demo")
    ai_line = ("SCRIPTED FAKE MODELS (nvplan.ai.fake) - no network, no cost (the default; --live for the real model)"
               if fake else
               f"LIVE MODEL {config.AI_MODEL} (--live) - REAL TOKENS ARE BEING SPENT on this run, which takes minutes")
    out(f"DB: {db_path}   AI: {ai_line}")
    out(RULE)

    # ---- 1. fresh db, seed, ingest -------------------------------------------------
    _section(out, 1, "Fresh database, categories, illustrative actuals")
    path = Path(db_path)
    if path.exists():
        path.unlink()
    engine = init_db(get_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False}))
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        seed_categories(s)
        df = load_actuals_csv(config.DATA_DIR / "actuals.csv")
        n = ingest_actuals(s, df)
        n_notes = q.seed_external_notes(s)
        labels = sorted(set(df["source_label"]))
        out(f"ingested {n} actual rows from {config.DATA_DIR / 'actuals.csv'} "
            f"({df['year'].min()}-{df['year'].max()}, {df['category_code'].nunique()} series), {n_notes} external notes")
        if q.illustrative_flag(s):
            out(f"!! WARNING: every actual carries source_label={labels} - generated dummy data, NOT Newvision figures. "
                f"The flag travels with every trace and grid.")

    # ---- 2. plan ---------------------------------------------------------------------
    _section(out, 2, "run_plan: OLS per cost category, valorized default revenue, base/best/worst, P&L -> BS -> CF")
    with factory() as s:
        run1 = run_plan(s, created_by="demo")
        out(f"scenarios {run1.scenario_ids}, {run1.n_plan_values} plan values, {run1.n_statement_lines} statement lines, "
            f"{run1.n_derivations} derivations (one per number)")
        grid = q.plan_grid(s, "base")
        out(f"\nBase plan grid - {grid['scenario_label']}  [illustrative={grid['illustrative']}]  (k EUR)")
        _print_grid(grid, out)
        out("\nParameters: PlanCost_c,t = alpha_c*(1+v_c)^(t-t0) + beta_c*PlanRevenue_t   (OLS window "
            f"{config.REGRESSION_WINDOW[0]}-{config.REGRESSION_WINDOW[1]})")
        _print_parameters(grid, out)

    # ---- 3. trace ------------------------------------------------------------------
    _section(out, 3, "Click a number: Personnel costs 2028, base - full derivation")
    with factory() as s:
        pv = find_plan_value(s, scenario_kind="base", category_code="PERS", year=2028)
        out(render_trace(trace_plan_value(s, pv.id)))

    # ---- 4. backtest ---------------------------------------------------------------
    _section(out, 4, "Backtest: rolling 5-year windows, horizons 1-3, MAPE / RMSE vs the 5-8 % thresholds")
    with factory() as s:
        out(q.backtest_report(s)["markdown"])

    # ---- 5. AI ---------------------------------------------------------------------
    _section(out, 5, "AI touchpoints 1 + 2: environmental scan, then a revenue proposal for "
             f"{PROPOSAL_YEAR} (advisory, status=proposed)")
    if fake:
        out("!! AI: SCRIPTED FAKE MODELS from nvplan.ai.fake (the default; pass --live for the real model). The tool "
            "calls, the validation, the stored prompt and the gate are real; the model's words are scripted.")
    else:
        out(f"AI: real model {config.AI_MODEL} (deepagents + langchain-anthropic) - REAL TOKENS ARE BEING SPENT")

    scan = run_env_scan(factory, model=scripted_env_scan_model() if fake else None)
    with factory() as s:
        scan_d = q.ai_record_dict(s, s.get(AiRecord, scan.id))
        out(f"\nenv scan -> ai_record #{scan_d['id']} status={scan_d['status']} model={scan_d['model_version']}, "
            f"{len(scan_d['note_ids'])} external notes written (source=ai_scan, ids {scan_d['note_ids']})")
        out(f"  summary: {scan_d['rationale']}")
        default = q.default_revenue(s, "base", PROPOSAL_YEAR)
        contract_notes = q.note_ids(s, category_code="REV", year=PROPOSAL_YEAR) or q.note_ids(s)
    out(f"\nvalorized default revenue {PROPOSAL_YEAR} (base): {default:,.1f} k EUR")

    if fake:
        proposed = round(default * (1 - 0.045), 1)
        rationale = (f"Note {contract_notes[0]}: the major client contract (~9% of revenue) ends Q2 {PROPOSAL_YEAR} and renewal is "
                     f"uncertain, so about half a year of it is at risk: {default:,.1f} * (1 - 0.045) = {proposed:,.1f} k EUR. "
                     "The public-sector framework agreement (env scan) supports the rest of the default path.")
        model = scripted_revenue_model(year=PROPOSAL_YEAR, proposed_value=proposed, rationale=rationale,
                                       cited_note_ids=[contract_notes[0]],
                                       flagged_factors=[f"client contract ends Q2 {PROPOSAL_YEAR}"])
    else:
        model = None
    rec = run_revenue_proposal(factory, scenario_kind="base", year=PROPOSAL_YEAR, default_value=default, model=model)
    with factory() as s:
        rec_d = q.ai_record_dict(s, s.get(AiRecord, rec.id))
    out(f"\nrevenue proposal -> ai_record #{rec_d['id']}  status={rec_d['status']}  proposed_value={rec_d['proposed_value']:,.1f} "
        f"k EUR ({(rec_d['proposed_value'] / default - 1) * 100:+.1f}% vs default)  model={rec_d['model_version']}")
    out(f"  rationale: {rec_d['rationale']}")
    out("  prompt (verbatim, excerpt):")
    out(_excerpt(rec_d["prompt_text"]))
    with factory() as s:
        pv = find_plan_value(s, scenario_kind="base", category_code="REV", year=PROPOSAL_YEAR)
        out(f"  plan_value REV {PROPOSAL_YEAR} base is still {pv.value:,.1f} (path {pv.path.value}) - nothing enters the plan unconfirmed")

    # ---- 6. confirm + rerun ---------------------------------------------------------
    _section(out, 6, f"Human gate: {CONFIRMER} confirms proposal #{rec.id} -> rerun, cascade through beta")
    with factory() as s:
        run2 = confirm_proposal(s, rec.id, confirmed_by=CONFIRMER)
        out(f"new scenarios {run2.scenario_ids} ({run2.label}); the first run is untouched (append-only)")
        grid2 = q.plan_grid(s, "base")
        out(f"\nBase grid, REV and PERS rows - {grid2['scenario_label']}  [illustrative={grid2['illustrative']}]")
        _print_grid(grid2, out, codes=["REV", "PERS"])
        c_old = grid["rows"][0]["cells"][PROPOSAL_YEAR]["value"]
        c_new = grid2["rows"][0]["cells"][PROPOSAL_YEAR]["value"]
        p_old = next(r for r in grid["rows"] if r["category_code"] == "PERS")["cells"][PROPOSAL_YEAR]["value"]
        p_new = next(r for r in grid2["rows"] if r["category_code"] == "PERS")["cells"][PROPOSAL_YEAR]["value"]
        beta = next(p for p in grid2["parameters"] if p["category_code"] == "PERS")["beta"]
        out(f"\nREV {PROPOSAL_YEAR}: {c_old:,.1f} -> {c_new:,.1f} ({c_new - c_old:+,.1f});  PERS {PROPOSAL_YEAR}: {p_old:,.1f} -> {p_new:,.1f} "
            f"({p_new - p_old:+,.1f} = beta {beta:.4f} * {c_new - c_old:+,.1f})")
        out(f"\nTrace of Personnel costs {PROPOSAL_YEAR} base - the revenue input now carries the ai block:")
        pv = find_plan_value(s, scenario_kind="base", category_code="PERS", year=PROPOSAL_YEAR)
        out(render_trace(trace_plan_value(s, pv.id)))

    # ---- 7. statements + deviation explanation ---------------------------------------
    _section(out, 7, "Statement consistency of the new base scenario, then AI touchpoint 3 on the backtest year "
             f"{BACKTEST_YEAR}")
    with factory() as s:
        scen = q.latest_scenario(s, "base")
        bs = q.statement_grid(s, "base", "bs")
        problems = bs["consistency"]
        rows = []
        for y in bs["years"]:
            cells = {r["line_code"]: r["cells"][y]["value"] for r in bs["rows"] if y in r["cells"]}
            rows.append([str(y), cells["cash"], cells["total_assets"], cells["total_liabilities_equity"],
                         cells["total_assets"] - cells["total_liabilities_equity"]])
        out(f"Balance sheet, scenario #{scen.id} ({scen.label}):")
        _table(["year", "cash", "total_assets", "total_liab_equity", "diff"], rows, out)
        out("consistency: " + ("OK - BS balances every year, cash and equity tie to the CF" if not problems else "; ".join(problems)))

        out(f"\nDeviation explanation needs a plan for a year with actuals: persisting a plan fitted on "
            f"{BACKTEST_YEAR - 5}-{BACKTEST_YEAR - 1} for {BACKTEST_YEAR}-{BACKTEST_YEAR + 4} (labelled as backtest fit).")
        run3 = run_backtest_year_plan(s, BACKTEST_YEAR, created_by="demo")
        out(f"scenarios {run3.scenario_ids} ({run3.label})")
        pva = plan_vs_actual(s, "base", BACKTEST_YEAR)
        out(f"\nPlan vs actual {BACKTEST_YEAR} (arithmetic, what the model is given):")
        _table(["category", "plan", "actual", "deviation", "dev %", "revenue-driven", "residual"],
               [[r["category_code"], r["plan"], r["actual"], r["deviation"], f"{r['deviation_pct']:+.2f}",
                 r.get("revenue_driven_part", ""), r.get("residual", "")] for r in pva["rows"]], out)
    if fake:
        rev = next(r for r in pva["rows"] if r["category_code"] == "REV")
        contribs = []
        for r in pva["rows"]:
            if r["category_code"] == "REV":
                expl = f"revenue came in at {r['actual']:,.1f} vs plan {r['plan']:,.1f} ({r['deviation']:+,.1f}, {r['deviation_pct']:+.1f}%)"
            else:
                expl = (f"{r['deviation']:+,.1f} = beta {r['beta']:.4f} * revenue deviation {rev['deviation']:+,.1f} "
                        f"= {r['revenue_driven_part']:+,.1f} revenue-driven, residual {r['residual']:+,.1f} on the fixed part")
            contribs.append({"category_code": r["category_code"], "plan": r["plan"], "actual": r["actual"],
                             "deviation": r["deviation"], "explanation": expl})
        largest = max(contribs, key=lambda c: abs(c["deviation"]))
        summary = (f"{BACKTEST_YEAR} revenue deviated {rev['deviation']:+,.1f} k EUR ({rev['deviation_pct']:+.1f}%) from the valorized "
                   f"default; the largest cost deviation is {largest['category_code']} ({largest['deviation']:+,.1f}); "
                   f"per category the beta-driven part is separated from the residual on the fixed part.")
        dev_model = scripted_deviation_model(scenario_kind="base", year=BACKTEST_YEAR, summary=summary, contributions=contribs)
    else:
        dev_model = None
    dev = run_deviation_explanation(factory, scenario_kind="base", year=BACKTEST_YEAR, model=dev_model)
    with factory() as s:
        dev_d = q.ai_record_dict(s, s.get(AiRecord, dev.id))
    out(f"\ndeviation explanation -> ai_record #{dev_d['id']} status={dev_d['status']} model={dev_d['model_version']} "
        "(no proposed value; advisory only)")
    out(f"  summary: {dev_d['rationale']}")
    structured = dev_d["response_text"].split("---STRUCTURED---", 1)[-1].strip()
    out("  structured response (verbatim):")
    out("\n".join(f"  | {ln}" for ln in structured.splitlines()[:40]))

    # ---- 8. counts -----------------------------------------------------------------
    _section(out, 8, "Row counts per table")
    with factory() as s:
        counts = q.table_counts(s)
    _table(["table", "rows"], [[k, v] for k, v in counts.items()], out)
    out(f"\ndone in {time.perf_counter() - t_start:.1f} s  ->  {path}   (serve it: uv run nvplan-serve --db sqlite:///{path})")
    return counts


if __name__ == "__main__":
    sys.exit(main())
