"""bridge/sweep_check.py — prove B4 end to end (PLATFORM.md §12.5, §12.6, work package B4).
Console script: ``nvplan-brain-sweep-check``.

The §12.6 done-when is explicit: "it finds a tripped reversal condition on the illustrative data
and reports it." None of the three decisions already sitting in the real ``brain/`` trips
mechanically today (the personnel-cost-assumption decision's own R-squared threshold of 0.6 is
nowhere near the illustrative PERS regression's real fit of ~0.999) - so this script does not
hand-author a fixture to make the done-when pass. Instead it drives the record through the real
code path the task calls for: B3's ``draft_decision`` (via ``nvplan.ai.assistant.ask()``, scripted
offline exactly like ``bridge/draft_check.py``), then the one human act PLATFORM.md §12.2 allows -
a direct one-line status edit - promotes it to ``decided``. Only then does
``bridge.sweep.evaluate_reversal_conditions`` check it, against the real illustrative baseline
plan (the same ``data/illustrative/actuals.csv`` every other bridge check reads).

    1. seed categories, ingest illustrative actuals, run the baseline plan - the same real REV
       2027 figure ``bridge/check.py`` and ``bridge/draft_check.py`` both read.
    2. the assistant drafts a decision whose reversal condition names that exact checkable
       quantity ("planned REV for 2027 falls below <threshold>"), with a threshold safely above
       the real baseline - a real, mechanically evaluable condition, not a rigged one.
    3. reindex; confirm the draft landed at pending. Sweep it now: pending decisions carry no
       reversal verdict at all (PLATFORM.md §12.5 only asks about *decided* decisions).
    4. promote it by hand (a direct file edit, never through any writer function - exactly what a
       human reviewer does in git): status -> decided. No quantified-effect block is needed here;
       B3/the bridge already prove that direction end to end.
    5. reindex; sweep again. The sweep must report this decision's condition as TRIPPED, backed by
       the real plan figure - and must not have changed one byte of any brain/ file, nor one
       claim's status, in doing so.

Exit 0 on success; non-zero with a legible reason otherwise.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from nvplan import config as nvplan_config
from nvplan.ai.assistant import ask
from nvplan.ai.fake import FakeToolCallingModel, ai_calls, structured, tool_call
from nvplan.db.session import get_engine as nvplan_get_engine, seed_categories
from nvplan.ingest.actuals import ingest_actuals, load_actuals_csv
from nvplan.services.planning import run_plan
from nvplan.services.trace import find_plan_value

from provenance.models import Claim, ClaimKind

from bridge.db import init_platform_db
from brainkit.indexer import reindex_tree
from bridge.sweep import render_sweep_report, run_sweep

TODAY = "2026-09-20"
DECISION_SLUG = "commit-2027-revenue-floor"
DECISION_PATH = f"decisions/{TODAY}-{DECISION_SLUG}.md"
REVERSAL_YEAR = 2027
REVERSAL_THRESHOLD = 24_000.0  # kEUR - above the real illustrative baseline, so the condition trips

_DRAFT_ARGS = dict(
    slug=DECISION_SLUG,
    title="Commit to a 2027 revenue floor",
    date=TODAY,
    context="Sales wants a public commitment to a minimum 2027 revenue figure for the board deck.",
    options=["Make no public commitment", "Commit to a floor pinned to the current plan baseline"],
    decision="Commit to a 2027 revenue floor, tracked against the plan baseline every quarter.",
    why="A public commitment forces the floor to be checked against the actual plan, not restated from memory.",
    evidence=[["The current planning cycle has an established REV baseline for every plan year.", "(industry-knowledge)"]],
    reversal=f"If planned REV for {REVERSAL_YEAR} falls below {REVERSAL_THRESHOLD:.1f}, we would treat the floor as unrealistic and revisit the commitment.",
)


def _step(msg: str) -> None:
    print(f"\n=== {msg} ===")


def _snapshot(brain_root: Path, session: Session) -> tuple[dict[Path, bytes], dict[int, str | None]]:
    files = {p: p.read_bytes() for p in sorted(brain_root.rglob("*.md"))}
    statuses = {c.id: c.status for c in session.execute(select(Claim)).scalars().all()}
    return files, statuses


def main(argv: list[str] | None = None) -> int:
    with tempfile.TemporaryDirectory(prefix="nvplan-brain-sweep-check-") as tmp:
        brain_root = Path(tmp) / "brain"
        db_path = Path(tmp) / "platform.db"
        engine = nvplan_get_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
        init_platform_db(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False)

        with Session(engine) as session:
            _step("1. seed categories, ingest illustrative actuals, run the baseline plan")
            seed_categories(session)
            actuals_df = load_actuals_csv(nvplan_config.DATA_DIR / "actuals.csv")
            n_actuals = ingest_actuals(session, actuals_df)
            run_plan(session, created_by="sweep-check")
            baseline_rev = find_plan_value(session, scenario_kind="base", category_code="REV", year=REVERSAL_YEAR)
            print(f"ingested {n_actuals} actual rows; baseline REV {REVERSAL_YEAR} = {baseline_rev.value:,.1f} kEUR")
            if baseline_rev.value >= REVERSAL_THRESHOLD:
                print(
                    f"ASSERTION FAILED: baseline REV {REVERSAL_YEAR} ({baseline_rev.value:,.1f}) is not below the "
                    f"threshold ({REVERSAL_THRESHOLD:,.1f}) - this script's own condition would not trip"
                )
                return 1

            _step("2. the assistant drafts a decision whose reversal names that exact checkable figure")
            model = FakeToolCallingModel(
                responses=[
                    ai_calls(tool_call("draft_decision", _DRAFT_ARGS, "draft-1")),
                    structured(
                        "AssistantAnswer",
                        {
                            "segments": [{"type": "text", "text": "Drafted a pending decision for human review."}],
                            "proposal": None,
                            "ai_record_id": -1,
                            "usage": {},
                        },
                    ),
                ]
            )
            answer = ask(factory, "Should we commit to a 2027 revenue floor?", model=model, brain_root=brain_root)
            print(f"assistant answered; ai_record_id={answer.ai_record_id}")

            decision_path = brain_root / DECISION_PATH
            if not decision_path.exists():
                print(f"the draft was never written to {decision_path}")
                return 1
            print(f"wrote {decision_path.relative_to(brain_root.parent)}")

            _step("3. reindex; while pending, the sweep reports no reversal verdict for it")
            report = reindex_tree(session, brain_root, strict=True)
            print(f"files_seen={report.files_seen} indexed={report.indexed} skipped_unchanged={report.skipped_unchanged} rejected={len(report.rejected)}")
            if report.rejected:
                for path in report.rejected:
                    print(f"  REJECTED: {path}")
                return 1
            claim = session.execute(select(Claim).where(Claim.path == DECISION_PATH)).scalar_one_or_none()
            if claim is None or claim.status != "pending" or claim.kind is not ClaimKind.decision:
                print(f"expected a pending decision claim, got {claim!r}")
                return 1
            print(f"indexed as claim id={claim.id} status={claim.status}")

            pending_sweep = run_sweep(session, brain_root)
            if any(v.claim_id == claim.id for v in pending_sweep.reversal_verdicts):
                print("the pending decision already has a reversal verdict - it should not, it is not decided yet")
                return 1
            print("sweep while pending: no reversal verdict for this claim (correct - it is not decided yet)")

            _step("4. a human promotes it (never through any writer function - a direct file edit)")
            text = decision_path.read_text(encoding="utf-8")
            promoted = text.replace("## Status\npending", "## Status\ndecided", 1)
            if promoted == text:
                print("could not find the pending status line to promote - the file's shape changed?")
                return 1
            decision_path.write_text(promoted, encoding="utf-8")
            print(f"promoted {decision_path.name}: status -> decided")

            _step("5. reindex; the sweep now reports this decision's condition as TRIPPED")
            report = reindex_tree(session, brain_root, strict=True)
            print(f"files_seen={report.files_seen} indexed={report.indexed} skipped_unchanged={report.skipped_unchanged} rejected={len(report.rejected)}")
            if report.rejected:
                for path in report.rejected:
                    print(f"  REJECTED: {path}")
                return 1

            before_files, before_statuses = _snapshot(brain_root, session)
            sweep_report = run_sweep(session, brain_root)
            print(render_sweep_report(sweep_report))
            after_files, after_statuses = _snapshot(brain_root, session)

            if before_files != after_files:
                print("ASSERTION FAILED: run_sweep changed the bytes of at least one brain/ file")
                return 1
            if before_statuses != after_statuses:
                print("ASSERTION FAILED: run_sweep changed at least one claim's status")
                return 1
            print("\nnon-mutation check: every brain/ file's bytes and every claim's status are unchanged by the sweep")

            our_verdicts = [v for v in sweep_report.reversal_verdicts if v.claim_id == claim.id]
            if len(our_verdicts) != 1:
                print(f"expected exactly one reversal verdict for claim {claim.id}, got {len(our_verdicts)}")
                return 1
            verdict = our_verdicts[0]
            if verdict.mechanism != "plan_value" or verdict.tripped is not True:
                print(f"ASSERTION FAILED: expected a tripped plan_value verdict, got {verdict}")
                return 1

    print(
        "\nsweep check passed: a decision drafted and promoted through the real B3 code path has a "
        "reversal condition the sweep found tripped against the real illustrative baseline plan, "
        "reported without writing anything."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
