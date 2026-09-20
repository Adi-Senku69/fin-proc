"""bridge/draft_check.py — prove B3 end to end: a draft drives no figure while pending, and
drives the figure it names once a human promotes it (PLATFORM.md §12.1, §12.6, work package
B3). Console script: ``nvplan-brain-draft-check``.

This is the single run that is the whole safety argument of §12.1, in one sequence:

    "the model proposes, a named human confirms, and nothing unconfirmed reaches a number."

Everything up to promotion runs through the real production code path -
``nvplan.ai.assistant.ask()``, scripted with an offline fake model exactly like the rest of the
AI-layer test suite (no network, no credential, no live model call) - never a hand-authored
file. Promotion itself is not a code path at all (PLATFORM.md §12.2: "a person promotes it by
editing one line") - simulated here the only way it can happen for real: editing the markdown
file directly, exactly what a human reviewer does in git. Two edits, not one, because driving a
figure is a separate contract (PLATFORM.md §7.1) from being promoted: the status line flips to
``decided``, and a ``## Quantified effect`` block is added - a drafted decision never carries one
(``brainkit.writer.draft_decision`` refuses it unconditionally on a pending draft), so a human
adds it exactly when they promote and quantify the decision, not before.

Runs entirely against a fresh temporary brain/ tree and a fresh temporary SQLite database, makes
no network call and needs no API key - same discipline as ``bridge/check.py`` and
``bridge/ingest_check.py``:

    1. ``nvplan.ai.assistant.ask()`` drafts a decision from a question via ``draft_decision`` - a
       real ``decisions/<date>-<slug>.md`` file, status=pending, no ``## Quantified effect``.
    2. reindex; assert the file indexed to a ``Claim(kind=decision, status="pending")``.
    3. ``bridge.effects.decided_effects``/``revenue_override`` over the real illustrative plan:
       this decision contributes no override, and the baseline REV figure is what it always was.
    4. a human promotes it by editing the file directly: status -> decided, plus a
       ``## Quantified effect`` block for REV/2027.
    5. reindex again; the same ``decided_effects``/``revenue_override`` call now includes this
       decision, and rerunning the plan changes the REV figure to the quantified value exactly.

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
from bridge.effects import decided_effects, revenue_override

TODAY = "2026-09-20"
DECISION_SLUG = "sunset-legacy-importer"
DECISION_PATH = f"decisions/{TODAY}-{DECISION_SLUG}.md"
EFFECT_YEAR = 2027
EFFECT_VALUE = 21_500.0

_DRAFT_ARGS = dict(
    slug=DECISION_SLUG,
    title="Sunset the legacy CSV importer",
    date=TODAY,
    context="The legacy importer duplicates the new ingestion pipeline and increases support load.",
    options=["Keep both importers", "Sunset the legacy importer"],
    decision="Sunset the legacy importer.",
    why="The new pipeline has fully replaced it for two consecutive quarters.",
    evidence=[["The new pipeline has handled all import volume for two quarters.", "(industry-knowledge)"]],
    reversal="If the new pipeline's error rate exceeds 1% for two consecutive weeks.",
)


def _step(msg: str) -> None:
    print(f"\n=== {msg} ===")


def main(argv: list[str] | None = None) -> int:
    with tempfile.TemporaryDirectory(prefix="nvplan-brain-draft-check-") as tmp:
        brain_root = Path(tmp) / "brain"
        db_path = Path(tmp) / "platform.db"
        engine = nvplan_get_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
        init_platform_db(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False)

        with Session(engine) as session:
            _step("1. the assistant drafts a decision from a question")
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
            answer = ask(factory, "Should we sunset the legacy CSV importer?", model=model, brain_root=brain_root)
            print(f"assistant answered; ai_record_id={answer.ai_record_id}")

            decision_path = brain_root / DECISION_PATH
            if not decision_path.exists():
                print(f"the draft was never written to {decision_path}")
                return 1
            print(f"wrote {decision_path.relative_to(brain_root.parent)}")

            _step("2. reindex and confirm the draft landed at pending")
            from brainkit.indexer import reindex_tree

            report = reindex_tree(session, brain_root, strict=True)
            print(f"files_seen={report.files_seen} indexed={report.indexed} rejected={len(report.rejected)}")
            if report.rejected:
                for path in report.rejected:
                    print(f"  REJECTED: {path}")
                return 1
            claim = session.execute(select(Claim).where(Claim.path == DECISION_PATH)).scalar_one_or_none()
            if claim is None:
                print("the decision file did not index to a claim")
                return 1
            if claim.status != "pending" or claim.kind is not ClaimKind.decision:
                print(f"expected a pending decision claim, got status={claim.status!r} kind={claim.kind}")
                return 1
            print(f"indexed as claim id={claim.id} kind={claim.kind.value} status={claim.status}")

            _step("3. while pending, it drives no figure")
            seed_categories(session)
            actuals_df = load_actuals_csv(nvplan_config.DATA_DIR / "actuals.csv")
            n_actuals = ingest_actuals(session, actuals_df)
            print(f"ingested {n_actuals} actual rows")
            baseline = run_plan(session, created_by="draft-check")
            print(f"baseline plan: {baseline.n_plan_values} plan values")

            effects = decided_effects(session)
            if any(e.claim_id == claim.id for e in effects):
                print("the pending decision already appears in decided_effects() - it should not")
                return 1
            plan = revenue_override(effects)
            if EFFECT_YEAR in plan.overrides:
                print(f"an override for {EFFECT_YEAR} exists before promotion - it should not")
                return 1
            before_rev = find_plan_value(session, scenario_kind="base", category_code="REV", year=EFFECT_YEAR)
            print(f"REV {EFFECT_YEAR} while pending = {before_rev.value:,.1f} kEUR (no decision drives it)")

            _step("4. a human promotes it (never through any writer function - a direct file edit)")
            text = decision_path.read_text(encoding="utf-8")
            promoted = text.replace("## Status\npending", "## Status\ndecided", 1)
            if promoted == text:
                print("could not find the pending status line to promote - the file's shape changed?")
                return 1
            promoted += (
                "\n## Quantified effect\n"
                f"- category: REV\n- year: {EFFECT_YEAR}\n- value: {EFFECT_VALUE}\n- unit: kEUR\n"
            )
            decision_path.write_text(promoted, encoding="utf-8")
            print(f"promoted {decision_path.name}: status -> decided, added a REV {EFFECT_YEAR} quantified effect")

            _step("5. reindex; the same bridge call now drives the figure")
            report = reindex_tree(session, brain_root, strict=True)
            print(f"files_seen={report.files_seen} indexed={report.indexed} rejected={len(report.rejected)}")
            if report.rejected:
                for path in report.rejected:
                    print(f"  REJECTED: {path}")
                return 1
            session.expire_all()
            claim = session.get(Claim, claim.id)
            if claim.status != "decided" or not claim.effect_json:
                print(f"promotion did not index as expected: status={claim.status!r} effect_json={claim.effect_json!r}")
                return 1
            print(f"reindexed as claim id={claim.id} status={claim.status} effect_json={claim.effect_json}")

            effects = decided_effects(session)
            plan = revenue_override(effects)
            if EFFECT_YEAR not in plan.overrides or plan.overrides[EFFECT_YEAR].claim_id != claim.id:
                print(f"the promoted decision's REV effect for {EFFECT_YEAR} did not produce an override")
                return 1

            after_run = run_plan(
                session, created_by="draft-check", revenue_override=plan.overrides, label_suffix=" +decision"
            )
            after_rev = find_plan_value(
                session,
                scenario_kind="base",
                category_code="REV",
                year=EFFECT_YEAR,
                scenario_id=after_run.scenario_ids["base"],
            )
            print(f"REV {EFFECT_YEAR} after promotion = {after_rev.value:,.1f} kEUR (decision now drives it)")
            if abs(after_rev.value - EFFECT_VALUE) > 1e-6:
                print(f"ASSERTION FAILED: expected REV {EFFECT_YEAR} == {EFFECT_VALUE}, got {after_rev.value}")
                return 1
            if abs(after_rev.value - before_rev.value) < 1e-6:
                print("ASSERTION FAILED: promotion did not actually change the figure")
                return 1

    print(
        "\ndraft check passed: pending drove no figure; the same decision, promoted by a human "
        "and only then, drove the exact figure it names."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
