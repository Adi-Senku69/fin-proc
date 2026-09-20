"""Tests for bridge.ingest (work package B2, PLATFORM.md §12.4): the env-scan writes to the
brain, not just the database.

Three layers, matching how bridge/check.py structures its own coverage:

1. ``write_env_scan_ingestion`` in isolation - a real file lands under ``ingestion/market/`` and
   indexes to a ``Claim(kind=ClaimKind.ingestion)``; a session with no provenance tables degrades
   gracefully instead of raising.
2. ``nvplan.ai.agents.run_env_scan`` end to end - the scan's flagged positions become a real
   markdown file, and the ``ExternalNote`` rows the run also writes get pointed at the resulting
   claim (``ExternalNote.source_claim_id``), without breaking anything ``tests/test_ai_touchpoints.
   py`` already asserts about those same rows.
3. The B2 brain-write provider gate (config.AI_BRAIN_WRITE_PROVIDER): run_env_scan must not go
   live just because a credential is present and config.AI_PROVIDER is "auto" - the whole reason
   this repo's own .env key would otherwise be exactly wrong for a path that writes files.

``bridge/ingest_check.py``'s own console-script proof (a scan's file citable as a decision's
evidence, end to end) is exercised directly in tests/test_reachability.py-style fashion below too.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from nvplan.ai import run_env_scan
from nvplan.ai.fake import FakeToolCallingModel, read_skill, scripted_env_scan_model, structured
from nvplan.ai.schemas import EnvScanResult
from nvplan.db.models import ExternalNote
from nvplan.db.session import get_engine as nvplan_get_engine
from nvplan.db.session import init_db as nvplan_init_db

from provenance.models import Claim, ClaimKind, get_engine as provenance_get_engine, init_db as provenance_init_db

from bridge.ingest import ScanFinding, write_env_scan_ingestion
from bridge.ingest_check import main as ingest_check_main

from nvplan.ai.evals.fixtures import new_platform_db
from test_ai_fixtures import ai_db  # noqa: F401 (fixture)

FINDINGS = [
    ScanFinding(domain="D2 Economic", position="D2.P4 Wage growth", materiality="high", reasoning="Sector wage settlements are running above the plan's valorization rate."),
]


# --------------------------------------------------------------------------- 1. write_env_scan_ingestion alone


def test_write_env_scan_ingestion_writes_and_indexes(tmp_path: Path):
    engine = provenance_get_engine(f"sqlite:///{tmp_path / 'prov.db'}")
    provenance_init_db(engine)
    with Session(engine) as s:
        outcome = write_env_scan_ingestion(
            s,
            tmp_path / "brain",
            slug="env-scan-1",
            date="2026-09-20",
            title="Environmental scan 2026-09-20 (ai_record 1)",
            summary="Wage growth is outpacing plan.",
            findings=FINDINGS,
        )
        assert outcome.result.written is True
        assert outcome.result.path is not None
        assert outcome.result.path.exists()
        assert outcome.result.path == tmp_path / "brain" / "ingestion" / "market" / "2026-09-20-env-scan-1.md"

        text = outcome.result.path.read_text(encoding="utf-8")
        assert "# Environmental scan 2026-09-20 (ai_record 1)" in text
        assert "D2 Economic / D2.P4 Wage growth (high)" in text
        assert "(industry-knowledge)" in text

        assert outcome.claim_id is not None
        claim = s.get(Claim, outcome.claim_id)
        assert claim is not None
        assert claim.kind is ClaimKind.ingestion
        assert claim.path == "ingestion/market/2026-09-20-env-scan-1.md"


def test_write_env_scan_ingestion_degrades_gracefully_with_no_provenance_tables(tmp_path: Path):
    """A finance-only session (no claim/evidence/claim_link tables) must not blow up the scan:
    the file still gets written; indexing just can't happen in THIS session."""
    engine = nvplan_get_engine(f"sqlite:///{tmp_path / 'finance_only.db'}")
    nvplan_init_db(engine)
    factory = sessionmaker(bind=engine)
    with factory() as s:
        outcome = write_env_scan_ingestion(
            s, tmp_path / "brain", slug="env-scan-1", date="2026-09-20",
            title="t", summary="s", findings=FINDINGS,
        )
        assert outcome.result.written is True
        assert outcome.result.path.exists()
        assert outcome.claim_id is None
        # the session must still be usable afterwards (rolled back cleanly, not left broken)
        s.execute(select(1))


# --------------------------------------------------------------------------- 2. run_env_scan end to end


def test_run_env_scan_writes_a_brain_file_and_links_notes(tmp_path: Path):
    factory, _plans = new_platform_db(tmp_path / "db.sqlite")
    brain_root = tmp_path / "brain"

    rec = run_env_scan(factory, model=scripted_env_scan_model(), positions_subset=["D1", "D2"], brain_root=brain_root)

    market_dir = brain_root / "ingestion" / "market"
    files = list(market_dir.glob("*.md"))
    assert len(files) == 1, f"expected exactly one ingestion file, found {files}"
    assert files[0].name == f"env-scan-{rec.id}.md" or files[0].stem.endswith(f"env-scan-{rec.id}")

    with factory() as s:
        notes = s.scalars(select(ExternalNote).where(ExternalNote.ai_record_id == rec.id)).all()
        assert len(notes) == 2
        claim_ids = {n.source_claim_id for n in notes}
        assert len(claim_ids) == 1 and None not in claim_ids
        (claim_id,) = claim_ids
        claim = s.get(Claim, claim_id)
        assert claim is not None and claim.kind is ClaimKind.ingestion
        assert claim.path == f"ingestion/market/{files[0].name}"


def test_run_env_scan_writes_nothing_when_nothing_is_flagged(tmp_path: Path):
    factory, _plans = new_platform_db(tmp_path / "db.sqlite")
    model = FakeToolCallingModel(
        responses=[
            read_skill("env_scan"),
            structured("EnvScanResult", EnvScanResult(flagged=[], summary="Nothing material found.").model_dump()),
        ]
    )
    rec = run_env_scan(factory, model=model, positions_subset=["D1"], brain_root=tmp_path / "brain")
    assert rec.rationale.strip() == "Nothing material found."
    assert not (tmp_path / "brain" / "ingestion").exists()


def test_run_env_scan_still_satisfies_the_existing_touchpoint_contract(ai_db):
    """Same assertions tests/test_ai_touchpoints.py::test_env_scan_persists_record_and_linked_notes
    makes - B2 must not disturb any of them. ai_db has no provenance tables (test_ai_fixtures.ai_db
    uses nvplan.db.session.init_db alone), so this also exercises the graceful-degradation path
    under the exact fixture the rest of the touchpoint suite uses."""
    factory, _ = ai_db
    rec = run_env_scan(factory, model=scripted_env_scan_model(), positions_subset=["D1", "D2.P4"])
    with factory() as s:
        new_notes = s.scalars(select(ExternalNote).where(ExternalNote.ai_record_id == rec.id)).all()
        assert len(new_notes) == 2
        for n in new_notes:
            assert n.author == "ai:fake"
            # no provenance tables in this fixture's engine -> indexing degrades to None
            assert n.source_claim_id is None


# --------------------------------------------------------------------------- 3. the provider gate


def test_run_env_scan_stays_deterministic_with_a_credential_present(monkeypatch, tmp_path: Path):
    """The rationale this knob exists for: config.AI_PROVIDER == 'auto' (the default) plus a
    credential in the environment must NOT make run_env_scan go live, unlike the other two
    touchpoints - config.AI_BRAIN_WRITE_PROVIDER defaults to 'deterministic' independently of
    AI_PROVIDER and of whether a credential is resolvable."""
    from nvplan.ai import agents

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-b2-dummy")
    monkeypatch.setattr(agents.config, "AI_PROVIDER", "auto", raising=False)
    monkeypatch.setattr(agents.config, "AI_BRAIN_WRITE_PROVIDER", "deterministic", raising=False)
    assert agents.credentials_available() is True

    factory, _plans = new_platform_db(tmp_path / "db.sqlite")
    rec = run_env_scan(factory, positions_subset=["D2"], brain_root=tmp_path / "brain")
    assert rec.model_version == "deterministic-v1"


def test_run_env_scan_provider_override_can_be_forced_live(monkeypatch):
    """The other half: setting the knob to 'live' explicitly still raises MissingCredentials with
    no key - never a silent live attempt, and never a silent fallback either."""
    from nvplan.ai import agents

    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(agents, "_ant_cli", lambda: None)
    monkeypatch.setattr(agents.config, "AI_BRAIN_WRITE_PROVIDER", "live", raising=False)
    with pytest.raises(agents.MissingCredentials):
        agents.resolve_model(None, provider=agents.config.AI_BRAIN_WRITE_PROVIDER)


# --------------------------------------------------------------------------- 4. the end-to-end console-script proof


def test_ingest_check_script_passes():
    """nvplan-brain-write-check (bridge/ingest_check.py): a scan produces a file, the file
    indexes to a claim, and a decision can cite it as resolved evidence - run for real here so a
    regression fails a normal test run, not only a manual invocation of the console script."""
    assert ingest_check_main([]) == 0
