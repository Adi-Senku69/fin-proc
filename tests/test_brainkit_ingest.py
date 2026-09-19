"""Tests for brainkit.validate / brainkit.indexer (PLATFORM.md §9, §10 P1 done-when:
"a hand-written decision file indexes cleanly; a bad one is rejected with a precise
message; reindexing is idempotent").

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

from brainkit.indexer import reindex_tree, rebuild_index
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

        report = reindex_tree(prov_session, brain_root, strict=True)
        assert report.files_seen == 1
        assert report.indexed == 0
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


class TestReindexOnRealTree:
    def test_clean_tree_indexes(self, prov_session):
        report = reindex_tree(prov_session, BRAIN_ROOT, strict=True)
        assert report.rejected == ()
        assert report.indexed > 0
        assert report.indexed == report.files_seen  # nothing was already in this fresh DB

    def test_reindex_is_idempotent(self, prov_session):
        first = reindex_tree(prov_session, BRAIN_ROOT, strict=True)
        claims_after_first = {c.id: c.body_sha256 for c in prov_session.execute(select(Claim)).scalars().all()}
        evidence_count_first = len(prov_session.execute(select(Evidence)).scalars().all())

        second = reindex_tree(prov_session, BRAIN_ROOT, strict=True)
        claims_after_second = {c.id: c.body_sha256 for c in prov_session.execute(select(Claim)).scalars().all()}
        evidence_count_second = len(prov_session.execute(select(Evidence)).scalars().all())

        assert second.indexed == 0
        assert second.skipped_unchanged == first.files_seen
        assert claims_after_first == claims_after_second  # same ids, same content
        assert evidence_count_first == evidence_count_second

    def test_rebuild_after_editing_a_file_updates_the_row(self, tmp_brain: Path, prov_session):
        rebuild_index(prov_session, tmp_brain)
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

        rebuild_index(prov_session, tmp_brain)
        after = prov_session.execute(select(Claim).where(Claim.path == path_str)).scalar_one()
        assert after.body_sha256 != old_sha
        assert after.title is not None and after.title.endswith("(revised)")


class TestStoredEvidenceTextIsStripped:
    """Defect: `Evidence.text` used to store the raw row (tag included), duplicating
    `tag_raw` in every rendering. `_write_claim_body` must now store `RowParse.text`
    (the claim with its tag stripped and whitespace collapsed) instead."""

    def test_stored_text_never_ends_with_its_own_tag_raw(self, prov_session):
        reindex_tree(prov_session, BRAIN_ROOT, strict=True)
        rows = prov_session.execute(select(Evidence)).scalars().all()
        assert rows, "expected at least one evidence row from the real tree"
        for row in rows:
            assert not row.text.endswith(row.tag_raw), (
                f"evidence {row.id} text still ends with its own tag_raw: {row.text!r}"
            )
            # the substantive claim survives stripping - it isn't reduced to nothing
            assert row.text.strip() != ""

    def test_tag_raw_is_unchanged_and_exact(self, prov_session):
        reindex_tree(prov_session, BRAIN_ROOT, strict=True)
        sunset = prov_session.execute(
            select(Claim).where(Claim.slug == "2026-09-20-sunset-legacy-import")
        ).scalar_one()
        rows = prov_session.execute(select(Evidence).where(Evidence.claim_id == sunset.id)).scalars().all()
        verbal = next(r for r in rows if r.tag_kind == TagKind.stakeholder_verbal)
        assert verbal.tag_raw == "(stakeholder-verbal, Head of Support, 2026-09-15)"
        # the claim text is still present and substantive, just without the tag trailing it
        assert "support lead" in verbal.text.lower()
        assert verbal.tag_raw not in verbal.text

    def test_reindexing_an_already_indexed_tree_keeps_stripped_text(self, prov_session):
        reindex_tree(prov_session, BRAIN_ROOT, strict=True)
        second = reindex_tree(prov_session, BRAIN_ROOT, strict=True)
        assert second.indexed == 0  # unchanged files are skipped, not rewritten
        rows = prov_session.execute(select(Evidence)).scalars().all()
        for row in rows:
            assert not row.text.endswith(row.tag_raw)

    def test_rebuild_index_corrects_stale_raw_text_left_by_an_older_reindex(self, tmp_brain: Path, prov_session):
        """Simulate a database populated by the pre-fix `_write_claim_body` (which
        stored the raw row, tag and all): the file on disk is unchanged (same
        body_sha256), so a plain reindex's skip-unchanged fast path would never
        revisit it - only `rebuild_index()` (wipe + fresh reindex) can correct it, per
        the docstring on that fast path in `reindex_tree`."""
        rebuild_index(prov_session, tmp_brain)
        sunset = prov_session.execute(
            select(Claim).where(Claim.slug == "2026-09-20-sunset-legacy-import")
        ).scalar_one()
        row = prov_session.execute(
            select(Evidence).where(Evidence.claim_id == sunset.id, Evidence.tag_kind == TagKind.stakeholder_verbal)
        ).scalar_one()

        # Corrupt the stored text back to the old (raw-row) shape, same claim, same
        # body_sha256 on the Claim - exactly what a legacy row looks like.
        row.text = row.text + " " + row.tag_raw
        prov_session.commit()
        assert row.text.endswith(row.tag_raw)  # sanity: the corruption took

        # A plain reindex is not expected to fix this (unchanged hash -> skipped).
        reindex_report = reindex_tree(prov_session, tmp_brain, strict=True)
        assert reindex_report.skipped_unchanged == reindex_report.files_seen
        stale = prov_session.execute(select(Evidence).where(Evidence.id == row.id)).scalar_one()
        assert stale.text.endswith(stale.tag_raw)  # still stale - documents the limit

        # rebuild_index() wipes and reindexes fresh, which does fix it. Clear the
        # identity map first: rebuild_index's raw `delete(...)` statements bypass the
        # ORM, so the `row`/`stale` objects above are now stale Python references to
        # rows that no longer exist - without expiring them, SQLite's rowid reuse
        # after DELETE can collide with those cached identities once reindex_tree
        # re-adds a Claim/Evidence under the same id and warns about it (a
        # session-hygiene artifact of this test re-using one session across two
        # rebuilds, not a defect in indexer.py itself).
        prov_session.expunge_all()
        rebuild_index(prov_session, tmp_brain)
        fixed = prov_session.execute(
            select(Evidence).where(
                Evidence.claim_id.in_(
                    select(Claim.id).where(Claim.slug == "2026-09-20-sunset-legacy-import")
                ),
                Evidence.tag_kind == TagKind.stakeholder_verbal,
            )
        ).scalar_one()
        assert not fixed.text.endswith(fixed.tag_raw)


class TestComputedTagResolution:
    def test_unresolved_without_a_lookup(self, prov_session):
        reindex_tree(prov_session, BRAIN_ROOT, strict=True)
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
            reindex_tree(session, BRAIN_ROOT, strict=True, derivation_lookup=lambda key: 42 if key == "param:PERS" else None)
            computed_evidence = session.execute(select(Evidence).where(Evidence.tag_kind == TagKind.computed)).scalars().all()
            assert len(computed_evidence) == 1
            row = computed_evidence[0]
            assert row.resolved is True
            assert row.target_derivation_id == 42

    def test_quantifies_link_created_to_a_synthetic_computed_claim(self, prov_session):
        reindex_tree(prov_session, BRAIN_ROOT, strict=True)
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

        report = reindex_tree(prov_session, tmp_brain, strict=True)

        assert path in report.rejected
        assert any(f.code == "misplaced_record" for f in report.findings)
        path_str = str(path.relative_to(tmp_brain).as_posix())
        assert prov_session.execute(select(Claim).where(Claim.path == path_str)).scalars().all() == []
