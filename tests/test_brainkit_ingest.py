"""Tests for brainkit.validate / brainkit.ingest (PLATFORM.md §9, §10 P1 done-when:
"a hand-written decision file ingests; a bad one is rejected with a precise message;
re-ingest is idempotent").

``provenance`` is a fixed dependency built by a parallel agent against the same
PLATFORM.md contract; these tests import it for real and skip with a clear reason if
it isn't there yet, rather than inventing a local double inside the test suite.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytest.importorskip("provenance", reason="provenance/ (Phase P0) is not present yet")

from sqlalchemy import select
from sqlalchemy.orm import Session

from provenance import Claim, ClaimLink, Evidence, TagKind, get_engine, init_db

from brainkit.ingest import ingest_tree, rebuild
from brainkit.validate import validate_file, validate_tree

REPO_ROOT = Path(__file__).resolve().parents[1]
BRAIN_ROOT = REPO_ROOT / "brain"
INVALID_FIXTURE = BRAIN_ROOT / "_examples" / "invalid-orphan-evidence.md"


@pytest.fixture()
def prov_session():
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture()
def tmp_brain(tmp_path: Path) -> Path:
    """A private, editable copy of the real brain/ tree for tests that mutate files."""
    dest = tmp_path / "brain"
    shutil.copytree(BRAIN_ROOT, dest)
    return dest


class TestValidateTree:
    def test_real_tree_has_no_error_level_findings(self):
        findings = validate_tree(BRAIN_ROOT)
        errors = [f for f in findings if f.severity == "error"]
        assert errors == [], f"unexpected error-level findings: {[(f.path, f.code, f.message) for f in errors]}"

    def test_worked_decisions_have_zero_findings_at_all(self):
        adopt = BRAIN_ROOT / "decisions" / "2026-09-19-adopt-decision-provenance.md"
        personnel = BRAIN_ROOT / "decisions" / "2026-09-19-personnel-cost-assumption.md"
        assert validate_file(adopt, brain_root=BRAIN_ROOT) == []
        assert validate_file(personnel, brain_root=BRAIN_ROOT) == []


class TestOrphanEvidenceRejection:
    def test_invalid_fixture_content_is_rejected_under_strict(self, tmp_path: Path, prov_session):
        """brain/_examples/invalid-orphan-evidence.md is deliberately invalid, but it
        lives under _examples/ where findings are reported at warning severity at
        most (so the real tree still "validates clean" per PLATFORM.md §9). To prove
        the rejection mechanism itself has real teeth, this test places the same
        content in a genuine collection directory (decisions/), where the same
        orphan-evidence defect keeps its full error severity."""
        fixture_text = INVALID_FIXTURE.read_text()
        body = fixture_text.split("-->", 1)[1].lstrip("\n")

        brain_root = tmp_path / "brain"
        (brain_root / "decisions").mkdir(parents=True)
        (brain_root / "decisions" / "2026-01-01-bad-example.md").write_text(body)

        findings = validate_file(brain_root / "decisions" / "2026-01-01-bad-example.md", brain_root=brain_root)
        assert any(f.code == "orphan_evidence" and f.severity == "error" for f in findings)

        report = ingest_tree(prov_session, brain_root, strict=True)
        assert report.files_seen == 1
        assert report.ingested == 0
        assert len(report.rejected) == 1
        assert any(f.code == "orphan_evidence" for f in report.findings)
        assert prov_session.execute(select(Claim)).scalars().all() == []

    def test_missing_reversal_fires_on_decided_decision_with_vague_condition(self, tmp_path: Path):
        brain_root = tmp_path / "brain"
        (brain_root / "decisions").mkdir(parents=True)
        text = INVALID_FIXTURE.read_text().split("-->", 1)[1].lstrip("\n")
        path = brain_root / "decisions" / "2026-01-01-bad-example.md"
        path.write_text(text)

        findings = validate_file(path, brain_root=brain_root)
        assert any(f.code == "missing_reversal" and f.severity == "error" for f in findings)
        # sanity: the fixture's reversal condition is the literal rejected phrase
        assert "if things change" in text.lower()


class TestIngestOnRealTree:
    def test_clean_tree_ingests(self, prov_session):
        report = ingest_tree(prov_session, BRAIN_ROOT, strict=True)
        assert report.rejected == ()
        assert report.ingested > 0
        assert report.ingested == report.files_seen  # nothing was already in this fresh DB

    def test_ingest_is_idempotent(self, prov_session):
        first = ingest_tree(prov_session, BRAIN_ROOT, strict=True)
        claims_after_first = {c.id: c.body_sha256 for c in prov_session.execute(select(Claim)).scalars().all()}
        evidence_count_first = len(prov_session.execute(select(Evidence)).scalars().all())

        second = ingest_tree(prov_session, BRAIN_ROOT, strict=True)
        claims_after_second = {c.id: c.body_sha256 for c in prov_session.execute(select(Claim)).scalars().all()}
        evidence_count_second = len(prov_session.execute(select(Evidence)).scalars().all())

        assert second.ingested == 0
        assert second.skipped_unchanged == first.files_seen
        assert claims_after_first == claims_after_second  # same ids, same content
        assert evidence_count_first == evidence_count_second

    def test_rebuild_after_editing_a_file_updates_the_row(self, tmp_brain: Path, prov_session):
        rebuild(prov_session, tmp_brain)
        target = tmp_brain / "decisions" / "2026-09-19-adopt-decision-provenance.md"
        path_str = str(target.relative_to(tmp_brain).as_posix())

        before = prov_session.execute(select(Claim).where(Claim.path == path_str)).scalar_one()
        old_sha = before.body_sha256

        text = target.read_text()
        text = text.replace(
            "# Decision: Adopt a markdown-plus-index decision-provenance system for the brain",
            "# Decision: Adopt a markdown-plus-index decision-provenance system for the brain (revised)",
        )
        target.write_text(text)

        rebuild(prov_session, tmp_brain)
        after = prov_session.execute(select(Claim).where(Claim.path == path_str)).scalar_one()
        assert after.body_sha256 != old_sha
        assert after.title is not None and after.title.endswith("(revised)")


class TestComputedTagResolution:
    def test_unresolved_without_a_lookup(self, prov_session):
        ingest_tree(prov_session, BRAIN_ROOT, strict=True)
        computed_evidence = prov_session.execute(select(Evidence).where(Evidence.tag_kind == TagKind.computed)).scalars().all()
        assert len(computed_evidence) == 1
        row = computed_evidence[0]
        assert row.tag_kind == TagKind.computed
        assert row.resolved is False
        assert row.target_derivation_id is None

    def test_resolved_with_a_lookup_that_returns_an_id(self):
        engine = get_engine("sqlite:///:memory:")
        init_db(engine)
        with Session(engine) as session:
            ingest_tree(session, BRAIN_ROOT, strict=True, derivation_lookup=lambda key: 42 if key == "param:PERS" else None)
            computed_evidence = session.execute(select(Evidence).where(Evidence.tag_kind == TagKind.computed)).scalars().all()
            assert len(computed_evidence) == 1
            row = computed_evidence[0]
            assert row.resolved is True
            assert row.target_derivation_id == 42

    def test_quantifies_link_created_to_a_synthetic_computed_claim(self, prov_session):
        ingest_tree(prov_session, BRAIN_ROOT, strict=True)
        links = prov_session.execute(select(ClaimLink)).scalars().all()
        quantifies_links = [l for l in links if l.relation.value == "quantifies"]
        assert len(quantifies_links) == 1
        target_claim = prov_session.get(Claim, quantifies_links[0].to_claim_id)
        assert target_claim.kind.value == "computed"
        assert target_claim.path is None
        assert target_claim.slug == "computed:param:PERS"


class TestMisplacedRecordRejection:
    def test_misplaced_record_rejected_under_strict_creates_zero_claim_rows(self, tmp_brain: Path, prov_session):
        """A hypothesis-shaped file dropped in decisions/ (brainkit.validate's
        content-shape cross-check) must be refused exactly like an orphan-evidence
        file: a misfiled record can never become a Claim row (PLATFORM.md §4.2, §9.2)."""
        path = tmp_brain / "decisions" / "2026-01-01-misfiled-hypothesis.md"
        path.write_text(
            "# Hypotheses — misfiled\n\n"
            "## Meta\n"
            "- Feature: misfiled-hypothesis\n"
            "- Created: 2026-01-01\n"
            "- Last updated: 2026-01-01\n\n"
            "## Value risk\n"
            "### H-V1: something risky\n"
            "- **Origin:** proactive\n"
            "- **Confidence:** medium\n"
            "- **Evidence for:**\n"
            "  - a claim for the risk area (industry-knowledge)\n"
            "- **Evidence against:**\n"
            "  - a claim against the risk area (industry-knowledge)\n"
            "- **Status:** open\n"
        )

        report = ingest_tree(prov_session, tmp_brain, strict=True)

        assert path in report.rejected
        assert any(f.code == "misplaced_record" for f in report.findings)
        path_str = str(path.relative_to(tmp_brain).as_posix())
        assert prov_session.execute(select(Claim).where(Claim.path == path_str)).scalars().all() == []
