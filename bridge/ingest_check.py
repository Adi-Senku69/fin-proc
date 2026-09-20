"""bridge/ingest_check.py — prove the B2 write path end to end (PLATFORM.md §3, §4.1, §12.4).
Console script: ``nvplan-brain-write-check``.

Runs entirely against a fresh temporary brain/ tree and a fresh temporary SQLite database, makes
no network call and needs no API key - same discipline as ``bridge/check.py``:

    1. ``bridge.ingest.write_env_scan_ingestion`` drafts and indexes one env-scan finding as a
       ``brain/ingestion/market/`` record - a real file, not a database-only note - exactly the
       function ``nvplan.ai.agents.run_env_scan`` calls.
    2. assert the file exists on disk and indexed to a ``Claim(kind=ClaimKind.ingestion)`` row.
    3. ``brainkit.writer.draft_decision`` (the B1 substrate) cites that ingestion record as its
       evidence via an ``[ingestion/market/...]`` link tag (PLATFORM.md §4.1) - the shape a
       decision citing an env-scan finding as evidence actually takes.
    4. reindex the tree and assert the decision's evidence row resolved against the ingestion
       file on disk.

Exit 0 on success; non-zero with a legible reason otherwise.
"""

from __future__ import annotations

import sys
import tempfile
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from provenance.models import Claim, ClaimKind, Evidence, TagKind, get_engine, init_db

from bridge.ingest import ScanFinding, write_env_scan_ingestion
from brainkit.indexer import reindex_tree
from brainkit.writer import draft_decision

TODAY = date.today().isoformat()


def _step(msg: str) -> None:
    print(f"\n=== {msg} ===")


def main(argv: list[str] | None = None) -> int:
    with tempfile.TemporaryDirectory(prefix="nvplan-brain-write-check-") as tmp:
        brain_root = Path(tmp) / "brain"
        db_path = Path(tmp) / "provenance.db"
        engine = get_engine(f"sqlite:///{db_path}")
        init_db(engine)

        with Session(engine) as session:
            _step("1. the env-scan writes its flagged positions as an ingestion record")
            outcome = write_env_scan_ingestion(
                session,
                brain_root,
                slug="env-scan-1",
                date=TODAY,
                title=f"Environmental scan {TODAY} (ai_record 1)",
                summary="Wage growth is outpacing the plan's valorization rate in the personnel category.",
                findings=[
                    ScanFinding(
                        domain="D2 Economic",
                        position="D2.P4 Wage growth",
                        materiality="high",
                        reasoning="Sector wage settlements are running above the plan's valorization rate.",
                    )
                ],
            )
            if not outcome.result.written or outcome.result.path is None:
                print(f"ingestion record was refused: {[(f.code, f.message) for f in outcome.result.findings]}")
                return 1
            if not outcome.result.path.exists():
                print(f"draft_ingestion reported written=True but {outcome.result.path} does not exist on disk")
                return 1
            print(f"wrote {outcome.result.path.relative_to(brain_root.parent)}")
            if outcome.claim_id is None:
                print("write_env_scan_ingestion wrote the file but did not index it to a claim")
                return 1
            ingestion_claim = session.get(Claim, outcome.claim_id)
            if ingestion_claim is None or ingestion_claim.kind is not ClaimKind.ingestion:
                print(f"claim {outcome.claim_id} is not a Claim(kind=ingestion): {ingestion_claim}")
                return 1
            print(f"indexed as claim id={ingestion_claim.id} kind={ingestion_claim.kind.value} path={ingestion_claim.path}")

            _step("2. a decision cites the ingestion record as its evidence")
            ingestion_link = f"[ingestion/market/{outcome.result.path.name}](../ingestion/market/{outcome.result.path.name})"
            decision_result = draft_decision(
                brain_root,
                slug="raise-pers-cost-buffer",
                title="Raise the personnel cost buffer for 2027",
                date=TODAY,
                context="The environmental scan flagged above-plan wage growth for the personnel category.",
                options=["Keep the buffer unchanged", "Raise the buffer by one point"],
                decision="Raise the buffer by one point for 2027.",
                why="The scan's finding is material and bears directly on the personnel cost line.",
                evidence=[("Sector wage growth is running above the plan's valorization rate.", ingestion_link)],
                not_doing=[],
                reversal="If year-end wage settlement data comes in at or below the plan's valorization rate.",
            )
            if not decision_result.written or decision_result.path is None:
                print(f"decision draft was refused: {[(f.code, f.message) for f in decision_result.findings]}")
                return 1
            print(f"wrote {decision_result.path.relative_to(brain_root.parent)}")

            _step("3. reindex and confirm the citation resolves")
            report = reindex_tree(session, brain_root, strict=True)
            print(f"files_seen={report.files_seen} indexed={report.indexed} rejected={len(report.rejected)}")
            if report.rejected:
                for path in report.rejected:
                    print(f"  REJECTED: {path}")
                return 1

            decision_path_str = str(decision_result.path.resolve().relative_to(brain_root.resolve()).as_posix())
            decision_claim = session.execute(select(Claim).where(Claim.path == decision_path_str)).scalar_one_or_none()
            if decision_claim is None:
                print("the decision file did not index to a claim")
                return 1
            evidence = session.scalars(select(Evidence).where(Evidence.claim_id == decision_claim.id)).all()
            if not evidence:
                print("the decision claim has no evidence rows")
                return 1
            cite = evidence[0]
            print(
                f"decision claim id={decision_claim.id} evidence[0].tag_kind={cite.tag_kind.value} "
                f"target_path={cite.target_path!r} resolved={cite.resolved}"
            )
            if cite.tag_kind is not TagKind.ingestion or not cite.resolved:
                print("the decision's evidence row citing the ingestion record did not resolve")
                return 1

    print("\nbrain-write check passed: a scan produced a file, the file indexed to a claim, "
          "and a decision cited it as resolved evidence.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
