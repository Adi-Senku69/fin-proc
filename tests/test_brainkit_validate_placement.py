"""Tests closing the validation hole in brainkit.validate: a record's directory used
to be the *only* signal for which rules applied (`"decisions" in path.parts`), so a
decision or hypothesis file placed anywhere else silently got zero evidence
validation. PLATFORM.md §4.2/§9 says an unsourced claim can never exist — a file that
escapes validation entirely is the worst version of that failure.

These tests exercise the fix: content-shape sniffing (`brainkit.parse.
sniff_record_shape`) for any file whose directory doesn't already pin its record
type, a new `misplaced_record` finding when shape and directory disagree, and a new
`unmodeled_file` warning for content that matches no known record shape.

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

from provenance import Claim, get_engine, init_db

from brainkit.indexer import reindex_tree
from brainkit.validate import validate_file, validate_tree

REPO_ROOT = Path(__file__).resolve().parents[1]
BRAIN_ROOT = REPO_ROOT / "brain"

# An unsourced decision: every evidence-heading bullet lacks a provenance tag, and
# the reversal condition is one of the rejected vague phrases. Placed unchanged in
# decisions/, brain root, and source/ to prove the same content is validated
# identically everywhere except for the added misplaced_record finding.
UNSOURCED_DECISION_BODY = """# Decision: Ship the weekly digest without a batching option

## Status
decided

## Date
2026-01-01

## Context
A fixture context paragraph, not a real decision.

## Options considered
1. Ship as-is.
2. Add a batching toggle.

## Decision
Ship as-is.

## Why
Fixture reasoning text.

## Evidence
- Users churn faster when notifications arrive in daily batches instead of real time

## Explicitly NOT doing
- A configurable batching window

## What would reverse this
if things change

## Remaining ambiguities
None — this is a fixture.
"""

PLAIN_PROSE_BODY = """# Team offsite notes

Just a page of prose. No evidence heading, no bold evidence label, nothing that
looks like a decision or a hypothesis record.

## Attendees
- Alice
- Bob
"""

# The only "## Evidence" string in this file sits inside a fenced code block, so it
# must never be treated as a real decision-shaped heading.
FENCED_EVIDENCE_BODY = """# How the schema works

Here's what a decision file's evidence heading looks like, for onboarding:

```
## Evidence
- some example row (chat, no artifact)
```

That's illustration only, not a real record.
"""

HYPOTHESIS_BODY = """# Hypotheses — misfiled

## Meta
- Feature: misfiled-hypothesis
- Created: 2026-01-01
- Last updated: 2026-01-01

## Value risk
### H-V1: something risky
- **Origin:** proactive
- **Confidence:** medium
- **Evidence for:**
  - a claim for the risk area (industry-knowledge)
- **Evidence against:**
  - a claim against the risk area (industry-knowledge)
- **Status:** open
"""


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


def _codes(findings, severity: str | None = None) -> set[str]:
    if severity is None:
        return {f.code for f in findings}
    return {f.code for f in findings if f.severity == severity}


class TestDirectoryStillWinsInsideModeledCollections:
    def test_decisions_dir_unsourced_decision_error_set_unchanged(self, tmp_brain: Path):
        path = tmp_brain / "decisions" / "2026-01-01-unsourced-decision.md"
        path.write_text(UNSOURCED_DECISION_BODY)

        findings = validate_file(path, brain_root=tmp_brain)
        errors = [f for f in findings if f.severity == "error"]

        assert "misplaced_record" not in _codes(errors)
        assert "orphan_evidence" in _codes(errors)
        assert "missing_reversal" in _codes(errors)
        assert sum(1 for f in errors if f.code == "orphan_evidence") == 2


class TestMisplacedRecordOutsideModeledCollections:
    def test_brain_root_gets_misplaced_record_and_orphan_evidence(self, tmp_brain: Path):
        decisions_path = tmp_brain / "decisions" / "2026-01-01-unsourced-decision.md"
        decisions_path.write_text(UNSOURCED_DECISION_BODY)
        decisions_errors = _codes(validate_file(decisions_path, brain_root=tmp_brain), "error")

        root_path = tmp_brain / "2026-01-01-unsourced-decision.md"
        root_path.write_text(UNSOURCED_DECISION_BODY)
        root_errors = _codes(validate_file(root_path, brain_root=tmp_brain), "error")

        assert "misplaced_record" in root_errors
        assert "orphan_evidence" in root_errors
        # the misfiled copy carries every substantive error the correctly-filed
        # copy does, plus the new placement error.
        assert root_errors == decisions_errors | {"misplaced_record"}

    def test_source_dir_gets_misplaced_record_and_orphan_evidence(self, tmp_brain: Path):
        decisions_path = tmp_brain / "decisions" / "2026-01-01-unsourced-decision.md"
        decisions_path.write_text(UNSOURCED_DECISION_BODY)
        decisions_errors = _codes(validate_file(decisions_path, brain_root=tmp_brain), "error")

        source_path = tmp_brain / "source" / "2026-01-01-unsourced-decision.md"
        source_path.write_text(UNSOURCED_DECISION_BODY)
        source_errors = _codes(validate_file(source_path, brain_root=tmp_brain), "error")

        assert "misplaced_record" in source_errors
        assert "orphan_evidence" in source_errors
        assert source_errors == decisions_errors | {"misplaced_record"}

    def test_misplaced_record_message_names_file_shape_and_target_dir(self, tmp_brain: Path):
        path = tmp_brain / "2026-01-01-unsourced-decision.md"
        path.write_text(UNSOURCED_DECISION_BODY)

        findings = validate_file(path, brain_root=tmp_brain)
        misplaced = next(f for f in findings if f.code == "misplaced_record")
        assert path.name in misplaced.message
        assert "decision" in misplaced.message
        assert "decisions/" in misplaced.message


class TestUnmodeledFile:
    def test_plain_prose_at_brain_root_is_one_warning_zero_errors(self, tmp_brain: Path):
        path = tmp_brain / "offsite-notes.md"
        path.write_text(PLAIN_PROSE_BODY)

        findings = validate_file(path, brain_root=tmp_brain)
        errors = [f for f in findings if f.severity == "error"]
        warnings = [f for f in findings if f.severity == "warning"]

        assert errors == []
        assert len(warnings) == 1
        assert warnings[0].code == "unmodeled_file"

    def test_evidence_heading_only_inside_fenced_code_is_unmodeled_not_misplaced(self, tmp_brain: Path):
        path = tmp_brain / "schema-example.md"
        path.write_text(FENCED_EVIDENCE_BODY)

        findings = validate_file(path, brain_root=tmp_brain)
        codes = _codes(findings)

        assert "misplaced_record" not in codes
        errors = [f for f in findings if f.severity == "error"]
        assert errors == []
        assert codes == {"unmodeled_file"}


class TestCrossCheckInsideModeledCollections:
    def test_hypothesis_shaped_file_in_decisions_flags_misplaced_naming_hypotheses(self, tmp_brain: Path):
        path = tmp_brain / "decisions" / "2026-01-01-misfiled-hypothesis.md"
        path.write_text(HYPOTHESIS_BODY)

        findings = validate_file(path, brain_root=tmp_brain)
        misplaced = next(f for f in findings if f.code == "misplaced_record")
        assert misplaced.severity == "error"
        assert "hypothesis" in misplaced.message
        assert "hypotheses/" in misplaced.message


class TestRealTreeStillValidatesClean:
    def test_validate_tree_has_zero_error_level_findings(self):
        findings = validate_tree(BRAIN_ROOT)
        errors = [f for f in findings if f.severity == "error"]
        assert errors == [], f"unexpected error-level findings: {[(f.path, f.code, f.message) for f in errors]}"


class TestReindexRejectsMisplacedRecord:
    def test_misplaced_record_rejected_under_strict_zero_claim_rows(self, tmp_brain: Path, prov_session):
        path = tmp_brain / "decisions" / "2026-01-01-misfiled-hypothesis.md"
        path.write_text(HYPOTHESIS_BODY)

        report = reindex_tree(prov_session, tmp_brain, strict=True)

        assert path in report.rejected
        assert any(f.code == "misplaced_record" for f in report.findings)
        assert prov_session.execute(select(Claim).where(Claim.path.like("%misfiled-hypothesis%"))).scalars().all() == []
