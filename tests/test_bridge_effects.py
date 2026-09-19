"""Tests for bridge.effects and the PLATFORM.md §7.1 "## Quantified effect" block:
parsing (brainkit.parse), validation (brainkit.validate's effect_on_undecided /
bad_effect / effect_not_wired), strict-reindex rejection of a malformed effect, and the
bridge.effects API that turns decided effects into a revenue override.

``nvplan.services.planning.Override`` is a fixed dependency added by a parallel change
against the same PLATFORM.md §7.1 contract; the one test that needs it
(``test_revenue_override_...``) imports it for real and skips with a clear reason if it
isn't there yet, rather than inventing a local double for someone else's contract.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

pytest.importorskip("provenance", reason="provenance/ (Phase P0) is not present yet")
pytest.importorskip("brainkit", reason="brainkit/ (Phase P1) is not present yet")

from sqlalchemy import select
from sqlalchemy.orm import Session

from provenance.models import Claim, ClaimKind, get_engine, init_db

from brainkit.indexer import reindex_tree
from brainkit.parse import parse_decision_file
from brainkit.validate import validate_file

from bridge.effects import QuantifiedEffect, decided_effects, revenue_override

REVERSAL = "If the fixture metric drops below 10% by 2027-01-01, we would reconsider."

_TEMPLATE = """# Decision: {title}

## Status
{status}

## Date
2026-01-01

## Context
Test fixture context, not a real decision.

## Options considered
1. Option A.
2. Option B.

## Decision
Pick option A.

## Why
Fixture reasoning.

## Evidence
- A tagged evidence claim for the fixture (industry-knowledge)
- A second tagged evidence claim for the fixture (chat, no artifact)

## Explicitly NOT doing
- Not doing something else in this fixture (industry-knowledge)

## What would reverse this
{reversal}

## Remaining ambiguities
None - this is a fixture.

## Quantified effect
{effect_lines}
"""

VALID_REV_EFFECT = "- category: REV\n- year: 2027\n- value: 100.5\n- unit: kEUR"


def _write_decision(
    brain_root: Path, name: str, *, status: str, effect_lines: str, title: str | None = None,
    reversal: str = REVERSAL,
) -> Path:
    decisions = brain_root / "decisions"
    decisions.mkdir(parents=True, exist_ok=True)
    path = decisions / name
    path.write_text(
        _TEMPLATE.format(title=title or name, status=status, reversal=reversal, effect_lines=effect_lines)
    )
    return path


@pytest.fixture()
def prov_session():
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    with Session(engine) as session:
        yield session


# --------------------------------------------------------------------------- parsing


class TestQuantifiedEffectParsing:
    def test_effect_block_parses(self, tmp_path):
        path = _write_decision(tmp_path / "brain", "2026-01-01-parses.md", status="decided",
                               effect_lines=VALID_REV_EFFECT)
        parsed = parse_decision_file(path)
        assert parsed.effect is not None
        assert parsed.effect.category == "REV"
        assert parsed.effect.year == 2027
        assert parsed.effect.value == 100.5
        assert parsed.effect.unit == "kEUR"

    def test_effect_block_tolerates_whitespace_and_case_in_keys(self, tmp_path):
        lines = "-  Category :  REV\n-   YEAR:2027\n- Value :  100.5\n- UNIT : kEUR"
        path = _write_decision(tmp_path / "brain", "2026-01-01-tolerant.md", status="decided", effect_lines=lines)
        parsed = parse_decision_file(path)
        assert parsed.effect is not None
        assert parsed.effect.category == "REV"
        assert parsed.effect.year == 2027
        assert parsed.effect.value == 100.5
        assert parsed.effect.unit == "kEUR"

    def test_malformed_value_lands_in_raw_and_does_not_raise(self, tmp_path):
        lines = "- category: REV\n- year: not-a-year\n- value: not-a-number\n- unit: kEUR"
        path = _write_decision(tmp_path / "brain", "2026-01-01-malformed.md", status="decided", effect_lines=lines)
        parsed = parse_decision_file(path)  # must not raise
        assert parsed.effect is not None
        assert parsed.effect.year is None
        assert parsed.effect.value is None
        assert parsed.effect.raw["year"] == "not-a-year"
        assert parsed.effect.raw["value"] == "not-a-number"

    def test_no_block_is_none(self, tmp_path):
        path = _write_decision(tmp_path / "brain", "2026-01-01-none.md", status="decided", effect_lines="")
        # an empty "## Quantified effect" body still creates the section (heading present);
        # remove it entirely by writing a file without the heading at all.
        text = path.read_text().rsplit("\n\n## Quantified effect", 1)[0] + "\n"
        path.write_text(text)
        parsed = parse_decision_file(path)
        assert parsed.effect is None


# --------------------------------------------------------------------------- validation


class TestEffectValidation:
    def test_effect_on_undecided_fires_on_pending_decision(self, tmp_path):
        path = _write_decision(tmp_path / "brain", "2026-01-01-pending-effect.md", status="pending",
                               effect_lines=VALID_REV_EFFECT)
        findings = validate_file(path, brain_root=tmp_path / "brain")
        assert any(f.code == "effect_on_undecided" and f.severity == "error" for f in findings)

    def test_effect_on_undecided_does_not_fire_when_decided(self, tmp_path):
        path = _write_decision(tmp_path / "brain", "2026-01-01-decided-effect.md", status="decided",
                               effect_lines=VALID_REV_EFFECT)
        findings = validate_file(path, brain_root=tmp_path / "brain")
        assert not any(f.code == "effect_on_undecided" for f in findings)
        assert not any(f.code == "bad_effect" for f in findings)

    @pytest.mark.parametrize(
        "effect_lines,expected_substring",
        [
            ("- category: NOPE\n- year: 2027\n- value: 100.0\n- unit: kEUR", "unknown category"),
            ("- category: REV\n- year: 1999\n- value: 100.0\n- unit: kEUR", "outside the plan horizon"),
            ("- category: REV\n- year: 2027\n- value: not-a-number\n- unit: kEUR", "not numeric"),
            ("- category: REV\n- year: 2027\n- value: -5\n- unit: kEUR", "positive number"),
            ("- category: REV\n- year: 2027\n- value: 100.0\n- unit: EUR", "must be 'kEUR'"),
            ("- category: REV\n- year: 2027\n- value: 100.0", "missing required key"),
        ],
        ids=["unknown_category", "year_outside_horizon", "non_numeric_value", "non_positive_value",
             "wrong_unit", "missing_key"],
    )
    def test_bad_effect_fires_for_each_malformed_case(self, tmp_path, effect_lines, expected_substring):
        path = _write_decision(tmp_path / "brain", "2026-01-01-bad-effect.md", status="decided",
                               effect_lines=effect_lines)
        findings = validate_file(path, brain_root=tmp_path / "brain")
        bad = [f for f in findings if f.code == "bad_effect"]
        assert bad, f"expected a bad_effect finding, got: {[(f.code, f.message) for f in findings]}"
        assert any(f.severity == "error" for f in bad)
        assert any(expected_substring in f.message for f in bad)

    def test_effect_not_wired_warns_for_a_pers_effect(self, tmp_path):
        lines = "- category: PERS\n- year: 2027\n- value: 50.0\n- unit: kEUR"
        path = _write_decision(tmp_path / "brain", "2026-01-01-pers-effect.md", status="decided", effect_lines=lines)
        findings = validate_file(path, brain_root=tmp_path / "brain")
        assert any(f.code == "effect_not_wired" and f.severity == "warning" for f in findings)
        assert not any(f.code == "bad_effect" for f in findings)


# --------------------------------------------------------------------------- reindex rejection


class TestMalformedEffectRejectedAtReindex:
    def test_malformed_effect_rejected_under_strict_zero_claim_rows(self, tmp_path, prov_session):
        brain_root = tmp_path / "brain"
        _write_decision(brain_root, "2026-01-01-bad-effect.md", status="decided",
                        effect_lines="- category: NOPE\n- year: 2027\n- value: 100.0\n- unit: kEUR")
        report = reindex_tree(prov_session, brain_root, strict=True)
        assert report.indexed == 0
        assert len(report.rejected) == 1
        assert any(f.code == "bad_effect" for f in report.findings)
        assert prov_session.execute(select(Claim)).scalars().all() == []

    def test_valid_effect_is_indexed_on_a_decided_decision(self, tmp_path, prov_session):
        brain_root = tmp_path / "brain"
        _write_decision(brain_root, "2026-01-01-good-effect.md", status="decided", effect_lines=VALID_REV_EFFECT)
        report = reindex_tree(prov_session, brain_root, strict=True)
        assert report.indexed == 1
        claim = prov_session.execute(select(Claim)).scalar_one()
        assert claim.effect_json == {"category": "REV", "year": 2027, "value": 100.5, "unit": "kEUR"}


# --------------------------------------------------------------------------- reversal condition (PLATFORM.md §4.4)

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_BRAIN_ROOT = REPO_ROOT / "brain"
REAL_REVERSAL_SLUG = "2026-09-20-sunset-legacy-import"


class TestReversalConditionIndexing:
    def test_real_worked_example_reversal_condition_is_indexed(self, prov_session):
        """End-to-end round trip on the real worked example (PLATFORM.md §4.4): the
        ``## What would reverse this`` prose in brain/decisions/2026-09-20-sunset-legacy-
        import.md must land on the indexed Claim, whitespace-normalised."""
        report = reindex_tree(prov_session, REAL_BRAIN_ROOT, strict=True)
        assert report.rejected == ()
        claim = prov_session.execute(select(Claim).where(Claim.slug == REAL_REVERSAL_SLUG)).scalar_one()
        assert claim.reversal_condition is not None
        assert "10 currently-active" in claim.reversal_condition
        assert "\n" not in claim.reversal_condition  # whitespace-normalised, not raw markdown

    def test_reversal_condition_indexes_as_none_when_section_is_empty(self, tmp_path, prov_session):
        brain_root = tmp_path / "brain"
        path = _write_decision(
            brain_root, "2026-01-01-empty-reversal.md", status="pending",
            effect_lines=VALID_REV_EFFECT, reversal="",
        )
        # pending + a quantified effect block would fire effect_on_undecided (error) and
        # block strict reindex; the effect block is irrelevant to this test, so drop it.
        text = path.read_text().rsplit("\n\n## Quantified effect", 1)[0] + "\n"
        path.write_text(text)

        report = reindex_tree(prov_session, brain_root, strict=True)
        assert report.indexed == 1
        claim = prov_session.execute(select(Claim)).scalar_one()
        assert claim.reversal_condition is None


# --------------------------------------------------------------------------- decided_effects


class TestDecidedEffects:
    def test_returns_only_decided_claims_with_an_effect(self, prov_session):
        decided_with_effect = Claim(
            kind=ClaimKind.decision, slug="d1", title="Decided with effect", status="decided",
            date=datetime.date(2026, 1, 1), effect_json={"category": "REV", "year": 2027, "value": 100.0, "unit": "kEUR"},
        )
        pending_with_effect = Claim(
            kind=ClaimKind.decision, slug="d2", title="Pending with effect", status="pending",
            date=datetime.date(2026, 1, 2), effect_json={"category": "REV", "year": 2028, "value": 50.0, "unit": "kEUR"},
        )
        decided_without_effect = Claim(
            kind=ClaimKind.decision, slug="d3", title="Decided without effect", status="decided",
            date=datetime.date(2026, 1, 3), effect_json=None,
        )
        prov_session.add_all([decided_with_effect, pending_with_effect, decided_without_effect])
        prov_session.flush()

        effects = decided_effects(prov_session)
        assert {e.claim_id for e in effects} == {decided_with_effect.id}
        assert effects[0].category_code == "REV"
        assert effects[0].year == 2027
        assert effects[0].value == 100.0
        assert effects[0].decision_slug == "d1"
        assert effects[0].status == "decided"


# --------------------------------------------------------------------------- revenue_override

try:
    from nvplan.services.planning import Override as _Override  # noqa: F401
    _OVERRIDE_AVAILABLE = True
except ImportError:
    _OVERRIDE_AVAILABLE = False

_SKIP_REASON = (
    "nvplan.services.planning.Override (PLATFORM.md §7.1) has not been added yet by the "
    "parallel agent editing nvplan/services/planning.py; bridge.effects.revenue_override() "
    "cannot be exercised without it."
)


class TestRevenueOverride:
    @pytest.mark.skipif(not _OVERRIDE_AVAILABLE, reason=_SKIP_REASON)
    def test_maps_rev_only_and_newer_decision_wins_on_conflict(self):
        from bridge.effects import OverridePlan

        old = QuantifiedEffect(
            claim_id=1, decision_slug="old-decision", decision_title="Old", category_code="REV",
            year=2027, value=100.0, unit="kEUR", status="decided", decided_on=datetime.date(2026, 1, 1),
        )
        newer = QuantifiedEffect(
            claim_id=2, decision_slug="new-decision", decision_title="New", category_code="REV",
            year=2027, value=200.0, unit="kEUR", status="decided", decided_on=datetime.date(2026, 2, 1),
        )
        pers = QuantifiedEffect(
            claim_id=3, decision_slug="pers-decision", decision_title="Pers", category_code="PERS",
            year=2027, value=10.0, unit="kEUR", status="decided", decided_on=datetime.date(2026, 1, 1),
        )

        plan = revenue_override([old, newer, pers])

        assert isinstance(plan, OverridePlan)
        assert set(plan.overrides) == {2027}
        assert plan.overrides[2027].value == 200.0
        assert plan.overrides[2027].claim_id == 2
        assert plan.overrides[2027].label == "new-decision"
        assert plan.shadowed == (1,)

    @pytest.mark.skipif(not _OVERRIDE_AVAILABLE, reason=_SKIP_REASON)
    def test_order_independent_conflict_resolution(self):
        old = QuantifiedEffect(
            claim_id=10, decision_slug="old", decision_title="Old", category_code="REV",
            year=2029, value=1.0, unit="kEUR", status="decided", decided_on=datetime.date(2025, 1, 1),
        )
        newer = QuantifiedEffect(
            claim_id=11, decision_slug="new", decision_title="New", category_code="REV",
            year=2029, value=2.0, unit="kEUR", status="decided", decided_on=datetime.date(2025, 6, 1),
        )
        # newer listed first this time - result must be identical
        plan = revenue_override([newer, old])
        assert plan.overrides[2029].claim_id == 11
        assert plan.shadowed == (10,)

    @pytest.mark.skipif(not _OVERRIDE_AVAILABLE, reason=_SKIP_REASON)
    def test_two_calls_do_not_contaminate_each_other(self):
        """revenue_override is a pure function of its argument: a call that produces a
        shadowed claim must never leak into the shadowed tuple of an unrelated later
        call (the old ``revenue_override.last_shadowed`` function attribute could)."""
        old = QuantifiedEffect(
            claim_id=100, decision_slug="old", decision_title="Old", category_code="REV",
            year=2030, value=1.0, unit="kEUR", status="decided", decided_on=datetime.date(2025, 1, 1),
        )
        newer = QuantifiedEffect(
            claim_id=101, decision_slug="new", decision_title="New", category_code="REV",
            year=2030, value=2.0, unit="kEUR", status="decided", decided_on=datetime.date(2025, 6, 1),
        )
        first_plan = revenue_override([old, newer])
        assert first_plan.shadowed == (100,)

        no_conflict = QuantifiedEffect(
            claim_id=200, decision_slug="lone", decision_title="Lone", category_code="REV",
            year=2031, value=5.0, unit="kEUR", status="decided", decided_on=datetime.date(2025, 1, 1),
        )
        second_plan = revenue_override([no_conflict])
        assert second_plan.shadowed == ()
        # the first call's result is untouched by the second call having run
        assert first_plan.shadowed == (100,)
