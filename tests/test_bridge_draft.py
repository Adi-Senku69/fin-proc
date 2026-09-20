"""Tests for bridge.draft (work package B3, PLATFORM.md §12.6): the assistant drafts decisions
and hypotheses from a question.

Three layers, matching how tests/test_bridge_ingest.py structures its own coverage for B2:

1. ``draft_decision_and_index``/``draft_hypotheses_and_index`` in isolation - a real file lands
   under ``decisions/``/``hypotheses/`` and indexes to a ``Claim``; a session with no provenance
   tables degrades gracefully instead of raising.
2. The end-to-end console-script proof (``bridge/draft_check.py``) - the heart of B3: a drafted
   decision drives no figure while pending, and drives the exact figure it names once a human
   promotes it, run for real here so a regression fails a normal test run.
3. ``nvplan.ai.assistant.ask()``'s own drafting path is covered in tests/test_assistant.py (its
   own file); this module stays focused on the bridge/ substrate and the end-to-end proof.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from nvplan.db.session import get_engine as nvplan_get_engine
from nvplan.db.session import init_db as nvplan_init_db

from provenance.models import Claim, ClaimKind, get_engine as provenance_get_engine, init_db as provenance_init_db

from bridge.draft import draft_decision_and_index, draft_hypotheses_and_index
from bridge.draft_check import main as draft_check_main

DECISION_KWARGS: dict = dict(
    slug="sunset-legacy-importer",
    title="Sunset the legacy CSV importer",
    date="2026-09-20",
    context="The legacy importer duplicates the new ingestion pipeline.",
    options=["Keep both importers", "Sunset the legacy importer"],
    decision="Sunset the legacy importer.",
    why="The new pipeline has fully replaced it.",
    evidence=[("The new pipeline has handled all import volume for two quarters.", "(industry-knowledge)")],
    reversal="If the new pipeline's error rate exceeds 1% for two consecutive weeks.",
)

HYPOTHESES_KWARGS: dict = dict(
    feature_slug="faster-import",
    title="Faster CSV import",
    hypotheses=[
        {
            "risk": "value",
            "belief": "Users would import files twice as often if it took under a minute.",
            "origin": "proactive",
            "confidence": "medium",
            "evidence_for": [("Two support tickets this quarter cite import speed.", "(industry-knowledge)")],
            "evidence_against": [],
            "open_questions": ["Do we know the current import duration distribution?"],
        }
    ],
)


# --------------------------------------------------------------------------- 1. draft_*_and_index alone


def test_draft_decision_and_index_writes_and_indexes_at_pending(tmp_path: Path):
    engine = provenance_get_engine(f"sqlite:///{tmp_path / 'prov.db'}")
    provenance_init_db(engine)
    with Session(engine) as s:
        outcome = draft_decision_and_index(s, tmp_path / "brain", **DECISION_KWARGS)
        assert outcome.result.written is True
        assert outcome.result.path == tmp_path / "brain" / "decisions" / "2026-09-20-sunset-legacy-importer.md"
        text = outcome.result.path.read_text(encoding="utf-8")
        assert "## Status\npending" in text
        assert "## Quantified effect" not in text  # never rendered - see bridge.draft's own docstring

        assert outcome.claim_id is not None
        claim = s.get(Claim, outcome.claim_id)
        assert claim is not None
        assert claim.kind is ClaimKind.decision
        assert claim.status == "pending"
        assert claim.effect_json is None


def test_draft_hypotheses_and_index_writes_and_indexes_at_open(tmp_path: Path):
    engine = provenance_get_engine(f"sqlite:///{tmp_path / 'prov.db'}")
    provenance_init_db(engine)
    with Session(engine) as s:
        outcome = draft_hypotheses_and_index(s, tmp_path / "brain", **HYPOTHESES_KWARGS)
        assert outcome.result.written is True
        assert outcome.result.path == tmp_path / "brain" / "hypotheses" / "faster-import.md"
        text = outcome.result.path.read_text(encoding="utf-8")
        assert "**Status:** open" in text
        assert "**Status:** supported" not in text

        assert outcome.claim_id is not None
        claim = s.get(Claim, outcome.claim_id)
        assert claim is not None
        assert claim.kind is ClaimKind.hypothesis


def test_draft_decision_and_index_degrades_gracefully_with_no_provenance_tables(tmp_path: Path):
    """A finance-only session (no claim/evidence/claim_link tables) must not blow up a draft:
    the file still gets written; indexing just can't happen in THIS session - same discipline
    bridge.ingest.write_env_scan_ingestion already applies for B2."""
    engine = nvplan_get_engine(f"sqlite:///{tmp_path / 'finance_only.db'}")
    nvplan_init_db(engine)
    factory = sessionmaker(bind=engine)
    with factory() as s:
        outcome = draft_decision_and_index(s, tmp_path / "brain", **DECISION_KWARGS)
        assert outcome.result.written is True
        assert outcome.result.path.exists()
        assert outcome.claim_id is None
        s.execute(select(1))  # the session must still be usable afterwards


def test_draft_decision_and_index_never_writes_an_effect_and_never_takes_a_status(tmp_path: Path):
    """bridge.draft.draft_decision_and_index exposes no ``effect``/``status`` parameter at all
    (see its own docstring) - a caller cannot pass either even by accident; this documents the
    contract as a test, not just a comment."""
    import inspect

    sig = inspect.signature(draft_decision_and_index)
    assert "effect" not in sig.parameters
    assert "status" not in sig.parameters


# --------------------------------------------------------------------------- 2. the end-to-end console-script proof


def test_draft_check_script_passes():
    """nvplan-brain-draft-check (bridge/draft_check.py): the heart of B3 - a drafted decision
    drives no figure while pending, and drives the exact figure it names once a human promotes
    it - run for real here so a regression fails a normal test run, not only a manual invocation
    of the console script."""
    assert draft_check_main([]) == 0
