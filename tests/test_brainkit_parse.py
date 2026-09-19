"""Tests for brainkit.parse (PLATFORM.md §5, P1 done-when: "a hand-written decision
file indexes cleanly"). Exercises the two worked decision examples and the worked
hypothesis example under brain/.

``provenance`` is a fixed dependency built by a parallel agent against the same
PLATFORM.md contract; these tests import it for real and skip with a clear reason if
it isn't there yet, rather than inventing a local double inside the test suite.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

pytest.importorskip("provenance", reason="provenance/ (Phase P0) is not present yet")

from provenance import ClaimKind

from brainkit.parse import parse_brain_file, parse_decision_file, parse_hypothesis_file

REPO_ROOT = Path(__file__).resolve().parents[1]
BRAIN_ROOT = REPO_ROOT / "brain"

ADOPT_DECISION = BRAIN_ROOT / "decisions" / "2026-09-19-adopt-decision-provenance.md"
PERSONNEL_DECISION = BRAIN_ROOT / "decisions" / "2026-09-19-personnel-cost-assumption.md"
HYPOTHESIS_FILE = BRAIN_ROOT / "hypotheses" / "decision-provenance-adoption.md"

REQUIRED_DECISION_HEADINGS = {
    "status",
    "date",
    "context",
    "options considered",
    "decision",
    "why",
    "evidence",
    "explicitly not doing",
    "what would reverse this",
    "remaining ambiguities",
}


class TestDecisionParsing:
    def test_adopt_decision_title_status_date(self):
        parsed = parse_decision_file(ADOPT_DECISION)
        assert parsed.title == "Decision: Adopt a markdown-plus-index decision-provenance system for the brain"
        assert parsed.status == "decided"
        assert parsed.date == "2026-09-19"
        assert parsed.kind is ClaimKind.decision
        assert parsed.errors == ()

    def test_adopt_decision_section_set(self):
        parsed = parse_decision_file(ADOPT_DECISION)
        headings = {s.heading.strip().lower() for s in parsed.sections}
        assert REQUIRED_DECISION_HEADINGS <= headings

    def test_adopt_decision_evidence_row_counts(self):
        parsed = parse_decision_file(ADOPT_DECISION)
        assert set(parsed.evidence_rows) == {"evidence_for", "not_doing"}
        assert len(parsed.evidence_rows["evidence_for"]) == 2
        assert len(parsed.evidence_rows["not_doing"]) == 2
        # each row survived soft-wrapping whole, with its tag intact
        for row in parsed.evidence_rows["evidence_for"] + parsed.evidence_rows["not_doing"]:
            assert "(" in row  # every real row carries a parenthetical or link tag

    def test_context_bullets_are_never_evidence(self):
        parsed = parse_decision_file(ADOPT_DECISION)
        context_body = next(s.body for s in parsed.sections if s.heading.strip().lower() == "context")
        assert context_body  # the Context section has real prose
        for rows in parsed.evidence_rows.values():
            for row in rows:
                assert row not in context_body

    def test_personnel_decision_cites_computed_tag(self):
        parsed = parse_decision_file(PERSONNEL_DECISION)
        assert parsed.status == "decided"
        all_rows = [r for rows in parsed.evidence_rows.values() for r in rows]
        assert any("(computed, param:PERS)" in r for r in all_rows)
        assert any("(industry-knowledge)" in r for r in all_rows)

    def test_sha256_is_stable_and_matches_raw_bytes(self):
        parsed_a = parse_decision_file(ADOPT_DECISION)
        parsed_b = parse_decision_file(ADOPT_DECISION)
        assert parsed_a.body_sha256 == parsed_b.body_sha256
        assert parsed_a.body_sha256 == hashlib.sha256(ADOPT_DECISION.read_bytes()).hexdigest()

    def test_dispatch_via_parse_brain_file(self):
        via_dispatch = parse_brain_file(ADOPT_DECISION)
        direct = parse_decision_file(ADOPT_DECISION)
        assert via_dispatch.title == direct.title
        assert via_dispatch.body_sha256 == direct.body_sha256
        assert via_dispatch.kind is ClaimKind.decision


class TestHypothesisParsing:
    def test_hypothesis_evidence_lists_map_to_right_sections(self):
        parsed = parse_hypothesis_file(HYPOTHESIS_FILE)
        assert parsed.kind is ClaimKind.hypothesis
        assert set(parsed.evidence_rows) == {"evidence_for", "evidence_against"}
        # two risk-area hypotheses, each with one evidence-for and one evidence-against
        # bullet -> four evidence lists total, correctly bucketed.
        assert len(parsed.evidence_rows["evidence_for"]) == 2
        assert len(parsed.evidence_rows["evidence_against"]) == 2
        for row in parsed.evidence_rows["evidence_for"]:
            assert "(intuition, PM, 2026-09-19)" in row
        for row in parsed.evidence_rows["evidence_against"]:
            assert "(industry-knowledge)" in row

    def test_hypothesis_dispatch_via_parse_brain_file(self):
        via_dispatch = parse_brain_file(HYPOTHESIS_FILE)
        assert via_dispatch.kind is ClaimKind.hypothesis
        assert via_dispatch.title == "Hypotheses — decision-provenance-adoption"

    def test_hypothesis_open_questions_are_not_evidence(self):
        parsed = parse_hypothesis_file(HYPOTHESIS_FILE)
        all_evidence_text = " ".join(r for rows in parsed.evidence_rows.values() for r in rows)
        # phrases unique to "Open questions / caveats:" bullets must never leak into evidence
        assert "false-positive rate" not in all_evidence_text
        assert "own tag form" not in all_evidence_text
        # the real evidence-against row (a genuine claim, not a caveat) is still present
        assert "support team" in all_evidence_text
