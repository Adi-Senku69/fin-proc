"""Tests for bridge.sweep (work package B4, PLATFORM.md §12.5, §12.6): the sweep.

Four layers:

1. Reversal-condition evaluation - the two mechanical templates (R-squared, planned plan value),
   each tripped and not-tripped, and the advisory fallback for a condition neither template can
   parse (never allowed to say "tripped"/"not tripped" - only ``mechanism in {"r_squared",
   "plan_value"}`` can).
2. The deterministic checks - two reused from ``brainkit.validate`` (``unresolved_link``,
   ``effect_not_wired``) and two new ones only a live session can compute
   (``computed_derivation_missing``, ``broken_supersession_chain``).
3. The central guardrail: a sweep run changes no file's bytes and no claim's status, and leaves
   the session with nothing pending to flush.
4. Reachability - ``bridge.sweep.run_sweep`` has two real, non-test callers
   (``bridge/sweep_check.py``, the console script; ``nvplan/api/app.py``'s ``/brain/sweep`` route)
   - plus the end-to-end console-script proof of the §12.6 done-when itself.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from nvplan import config as nvplan_config
from nvplan.ai.fake import deterministic_model
from nvplan.db.session import get_engine as nvplan_get_engine, seed_categories
from nvplan.ingest.actuals import ingest_actuals, load_actuals_csv
from nvplan.services.planning import run_plan

from provenance.models import Claim

from bridge.db import init_platform_db
from bridge.draft import draft_decision_and_index
from bridge.sweep import (
    BROKEN_SUPERSESSION_CHAIN,
    COMPUTED_DERIVATION_MISSING,
    REUSED_VALIDATE_CODES,
    evaluate_reversal_conditions,
    render_sweep_report,
    run_sweep,
)
from bridge.sweep_check import main as sweep_check_main
from brainkit.indexer import reindex_tree

REPO_ROOT = Path(__file__).resolve().parent.parent

DECISION_KWARGS: dict = dict(
    date="2026-09-20",
    context="Context for a sweep-test decision.",
    options=["Do nothing", "Do the thing"],
    decision="Do the thing.",
    why="Because the tests need a decision.",
    evidence=[("Some evidence for the decision.", "(industry-knowledge)")],
    reversal="If, by 2030, this decision has never been revisited, we would review it.",  # replaced per-test where relevant
)


def _platform(tmp_path: Path):
    """A fresh platform db (both metadatas) seeded with the real illustrative actuals and a
    baseline plan, plus an empty ``brain_root`` under the same tmp dir - the same setup
    ``bridge/sweep_check.py`` and ``bridge/check.py`` both build by hand."""
    engine = nvplan_get_engine(f"sqlite:///{tmp_path / 'platform.db'}", connect_args={"check_same_thread": False})
    init_platform_db(engine)
    session = Session(engine)
    seed_categories(session)
    actuals_df = load_actuals_csv(nvplan_config.DATA_DIR / "actuals.csv")
    ingest_actuals(session, actuals_df)
    run_plan(session, created_by="sweep-test")
    return session, tmp_path / "brain", engine


def _promote(brain_root: Path, rel_path: str, *, new_status: str, extra: str = "") -> None:
    """The one human act PLATFORM.md §12.2 allows: a direct one-line status edit, never through
    any writer function - exactly what ``bridge/draft_check.py``'s own promotion step does."""
    path = brain_root / rel_path
    text = path.read_text(encoding="utf-8")
    promoted = text.replace("## Status\npending", f"## Status\n{new_status}", 1)
    assert promoted != text, f"could not find the pending status line in {path}"
    path.write_text(promoted + extra, encoding="utf-8")


def _decide(session: Session, brain_root: Path, *, slug: str, extra: str = "", **overrides) -> Claim:
    """Draft (always pending), reindex, promote to decided, reindex again - returns the resulting
    Claim, expired-and-refetched so its fields reflect the promoted file."""
    kwargs = {"title": f"Decision: {slug}", **DECISION_KWARGS, **overrides, "slug": slug}
    outcome = draft_decision_and_index(session, brain_root, **kwargs)
    assert outcome.result.written, outcome.result.findings
    reindex_tree(session, brain_root, strict=True)
    _promote(brain_root, f"decisions/{kwargs['date']}-{slug}.md", new_status="decided", extra=extra)
    report = reindex_tree(session, brain_root, strict=True)
    assert not report.rejected, report.rejected
    claim = session.execute(select(Claim).where(Claim.path == f"decisions/{kwargs['date']}-{slug}.md")).scalar_one()
    return claim


# --------------------------------------------------------------------------- 1. reversal conditions


def test_r_squared_mechanism_trips_and_does_not(tmp_path):
    session, brain_root, _ = _platform(tmp_path)
    trips = _decide(
        session, brain_root, slug="r2-trips",
        reversal="If the R-squared for the MAT regression drops below 0.999, we would revisit this.",
    )
    holds = _decide(
        session, brain_root, slug="r2-holds",
        reversal="If the R-squared for the MAT regression drops below 0.5, we would revisit this.",
    )
    verdicts = {v.claim_id: v for v in evaluate_reversal_conditions(session, model=None)}

    assert verdicts[trips.id].mechanism == "r_squared"
    assert verdicts[trips.id].tripped is True

    assert verdicts[holds.id].mechanism == "r_squared"
    assert verdicts[holds.id].tripped is False


def test_plan_value_mechanism_trips_and_does_not(tmp_path):
    session, brain_root, _ = _platform(tmp_path)
    # The real illustrative baseline REV 2027 is ~23,418.9 kEUR (bridge/check.py, bridge/draft_check.py).
    trips = _decide(
        session, brain_root, slug="pv-trips",
        reversal="If planned REV for 2027 falls below 24000.0, we would revisit this.",
    )
    holds = _decide(
        session, brain_root, slug="pv-holds",
        reversal="If planned REV for 2027 falls below 1000.0, we would revisit this.",
    )
    verdicts = {v.claim_id: v for v in evaluate_reversal_conditions(session, model=None)}

    assert verdicts[trips.id].mechanism == "plan_value"
    assert verdicts[trips.id].tripped is True

    assert verdicts[holds.id].mechanism == "plan_value"
    assert verdicts[holds.id].tripped is False


def test_advisory_fallback_when_neither_template_matches(tmp_path):
    session, brain_root, _ = _platform(tmp_path)
    claim = _decide(
        session, brain_root, slug="advisory-only",
        reversal="If more than 10 customers churn in a quarter, we would revisit this.",
    )

    no_model = {v.claim_id: v for v in evaluate_reversal_conditions(session, model=None)}
    v = no_model[claim.id]
    assert v.mechanism == "advisory"
    assert v.tripped is None  # an advisory verdict never claims a condition tripped or held
    assert "needs a human" in v.detail

    with_model = {v.claim_id: v for v in evaluate_reversal_conditions(session, model=deterministic_model())}
    v2 = with_model[claim.id]
    assert v2.mechanism == "advisory"
    assert v2.tripped is None  # the model is consulted, but it never gets to decide this either
    assert "quarterly cadence" in v2.detail  # DeterministicChatModel reacts to "quarter" in the prose


def test_pending_decisions_get_no_reversal_verdict_at_all(tmp_path):
    session, brain_root, _ = _platform(tmp_path)
    kwargs = {
        **DECISION_KWARGS,
        "slug": "still-pending",
        "title": "Still pending",
        "reversal": "If planned REV for 2027 falls below 24000.0, we would revisit this.",
    }
    outcome = draft_decision_and_index(session, brain_root, **kwargs)
    assert outcome.result.written, outcome.result.findings
    reindex_tree(session, brain_root, strict=True)
    verdicts = evaluate_reversal_conditions(session, model=None)
    assert not any(v.claim_id == outcome.claim_id for v in verdicts)


# --------------------------------------------------------------------------- 2. deterministic checks


def test_computed_derivation_missing_when_the_key_does_not_resolve(tmp_path):
    session, brain_root, _ = _platform(tmp_path)
    outcome = draft_decision_and_index(
        session, brain_root, slug="cites-missing-derivation", title="Cites a missing derivation",
        **{**DECISION_KWARGS, "evidence": [("A figure nobody computed.", "(computed, param:NOPE)")]},
    )
    assert outcome.result.written, outcome.result.findings
    reindex_tree(session, brain_root, strict=True)

    report = run_sweep(session, brain_root, model=None)
    codes = {f.code for f in report.computed_derivation_findings}
    assert COMPUTED_DERIVATION_MISSING in codes
    assert any(f"claim {outcome.claim_id}" in f.message for f in report.computed_derivation_findings)


def test_computed_derivation_missing_does_not_flag_a_real_derivation(tmp_path):
    session, brain_root, _ = _platform(tmp_path)
    outcome = draft_decision_and_index(
        session, brain_root, slug="cites-real-derivation", title="Cites a real derivation",
        **{**DECISION_KWARGS, "evidence": [("The PERS regression fit.", "(computed, param:PERS)")]},
    )
    assert outcome.result.written, outcome.result.findings
    reindex_tree(session, brain_root, strict=True)

    report = run_sweep(session, brain_root, model=None)
    assert not any(f.code == COMPUTED_DERIVATION_MISSING for f in report.computed_derivation_findings)


def test_broken_supersession_chain_when_nothing_supersedes_it(tmp_path):
    session, brain_root, _ = _platform(tmp_path)
    claim = _decide(session, brain_root, slug="orphan-superseded")
    # A second, direct edit: decided -> superseded, with no other file linking back to it.
    path = brain_root / "decisions" / "2026-09-20-orphan-superseded.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("## Status\ndecided", "## Status\nsuperseded", 1), encoding="utf-8")
    report_reindex = reindex_tree(session, brain_root, strict=True)
    assert not report_reindex.rejected, report_reindex.rejected

    report = run_sweep(session, brain_root, model=None)
    codes = {f.code for f in report.supersession_findings}
    assert BROKEN_SUPERSESSION_CHAIN in codes
    assert any(f"claim {claim.id}" in f.message for f in report.supersession_findings)


def test_structural_findings_reuse_unresolved_link_and_effect_not_wired(tmp_path):
    session, brain_root, _ = _platform(tmp_path)
    source_dir = brain_root / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    source_path = source_dir / "note.md"
    source_path.write_text("# A note\n\nSome raw material.\n", encoding="utf-8")

    outcome = draft_decision_and_index(
        session, brain_root, slug="stale-link", title="Cites a source that will vanish",
        **{
            **DECISION_KWARGS,
            "evidence": [("Cites a real source that will later vanish.", "[source/note.md](../source/note.md)")],
        },
    )
    assert outcome.result.written, outcome.result.findings

    _decide(
        session, brain_root, slug="mat-effect", title="Decided with a MAT effect",
        extra="\n## Quantified effect\n- category: MAT\n- year: 2027\n- value: 100.0\n- unit: kEUR\n",
    )
    reindex_tree(session, brain_root, strict=True)

    source_path.unlink()  # the target vanishes after the fact - the sweep re-checks disk live

    report = run_sweep(session, brain_root, model=None)
    codes = {f.code for f in report.structural_findings}
    assert "unresolved_link" in codes
    assert "effect_not_wired" in codes
    assert set(REUSED_VALIDATE_CODES) >= codes  # nothing outside the two reused codes leaks through


# --------------------------------------------------------------------------- 3. the central guardrail


def test_sweep_never_mutates_files_claim_status_or_the_session(tmp_path):
    session, brain_root, _ = _platform(tmp_path)
    _decide(
        session, brain_root, slug="mutation-check",
        reversal="If planned REV for 2027 falls below 24000.0, we would revisit this.",
    )

    before_files = {p: p.read_bytes() for p in sorted(brain_root.rglob("*.md"))}
    before_statuses = {c.id: c.status for c in session.execute(select(Claim)).scalars().all()}

    report = run_sweep(session, brain_root, model=None)
    assert any(v.tripped is True for v in report.reversal_verdicts)  # sanity: it actually found something
    render_sweep_report(report)  # must not raise on real data

    assert not session.new and not session.dirty and not session.deleted

    after_files = {p: p.read_bytes() for p in sorted(brain_root.rglob("*.md"))}
    after_statuses = {c.id: c.status for c in session.execute(select(Claim)).scalars().all()}
    assert before_files == after_files
    assert before_statuses == after_statuses


# --------------------------------------------------------------------------- 4. reachability


def test_run_sweep_has_two_real_non_test_callers():
    """bridge.sweep.run_sweep must be reachable from more than its own tests - the same miss
    tests/test_reachability.py guards against for the write substrate (build-but-orphaned), here
    for the read-only sweep: a console script (``bridge/sweep_check.py``) and the API's own
    composition layer (``nvplan/api/app.py``'s ``/brain/sweep`` route)."""
    import inspect

    from bridge import sweep_check as bridge_sweep_check
    from nvplan.api import app as api_app

    script_src = inspect.getsource(bridge_sweep_check)
    assert "run_sweep(" in script_src, "bridge/sweep_check.py no longer calls bridge.sweep.run_sweep"

    app_src = inspect.getsource(api_app)
    assert "run_sweep(" in app_src, "nvplan/api/app.py's /brain/sweep route no longer calls bridge.sweep.run_sweep"


def test_sweep_console_script_is_registered_in_pyproject():
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    assert data["project"]["scripts"].get("nvplan-brain-sweep-check") == "bridge.sweep_check:main"


def test_sweep_check_script_passes():
    """nvplan-brain-sweep-check (bridge/sweep_check.py): the §12.6 done-when itself - a decision
    drafted and promoted through the real B3 code path has a reversal condition the sweep finds
    tripped against the real illustrative baseline plan, run for real here so a regression fails a
    normal test run, not only a manual invocation of the console script."""
    assert sweep_check_main([]) == 0


def test_brain_sweep_api_route_is_reachable_and_finds_the_tripped_condition(tmp_path):
    from fastapi.testclient import TestClient

    from nvplan.api.app import create_app

    session, brain_root, engine = _platform(tmp_path)
    _decide(
        session, brain_root, slug="api-route-check",
        reversal="If planned REV for 2027 falls below 24000.0, we would revisit this.",
    )
    session.close()

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    app = create_app(session_factory=factory)
    with TestClient(app) as client:
        r = client.get("/brain/sweep", params={"brain_root": str(brain_root)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert any(v["tripped"] is True for v in body["reversal_verdicts"])
    assert set(body) == {
        "structural_findings", "computed_derivation_findings", "supersession_findings", "reversal_verdicts",
    }
