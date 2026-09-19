"""Tests for brainkit.writer — the write substrate (PLATFORM.md §12, phase B1).

This is the only sanctioned way to create a brain/ markdown file. Every test here
targets one of the five hard limits in PLATFORM.md §12.3 in isolation, plus the
integration checks phase B1 is "done when": a well-formed draft writes, validates
clean on the whole tree, indexes as a `pending` claim that `decided_effects` does not
pick up, a hypothesis draft round-trips through the parser, and an ingestion record
can be cited by a later decision draft.

All brain-tree fixtures live under `tmp_path` (a private copy of the real `brain/`
tree) — never the repository's own `brain/`, which stays untouched throughout.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytest.importorskip("provenance", reason="provenance/ (Phase P0) is not present yet")
pytest.importorskip("brainkit", reason="brainkit/ (Phase P1) is not present yet")

from sqlalchemy.orm import Session

from provenance import get_engine, init_db
from provenance.models import Claim, ClaimKind

from brainkit.indexer import reindex_tree
from brainkit.parse import parse_decision_file, parse_hypothesis_file
from brainkit.validate import validate_content, validate_tree
from brainkit.writer import DraftResult, HypothesisDraft, draft_decision, draft_hypotheses, draft_ingestion

from bridge.effects import decided_effects

REPO_ROOT = Path(__file__).resolve().parents[1]
BRAIN_ROOT = REPO_ROOT / "brain"


@pytest.fixture()
def tmp_brain(tmp_path: Path) -> Path:
    """A private, editable copy of the real brain/ tree. Never the repo's own brain/."""
    dest = tmp_path / "brain"
    shutil.copytree(BRAIN_ROOT, dest)
    return dest


@pytest.fixture()
def prov_session():
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    with Session(engine) as session:
        yield session


def _error_codes(result: DraftResult) -> set[str]:
    return {f.code for f in result.findings if f.severity == "error"}


# A minimal, valid decision draft's kwargs, reusable across tests.
def _decision_kwargs(**overrides) -> dict:
    base = dict(
        slug="widget-launch",
        title="Launch the widget",
        date="2026-03-01",
        context="Customers keep asking for a widget.",
        options=["Build it", "Don't"],
        decision="",
        why="",
        evidence=[("Three customers asked for it in the last month", "(chat, no artifact)")],
        not_doing=[("A fully custom widget builder", "(industry-knowledge)")],
        reversal="If fewer than 2 customers use it within 60 days of ship.",
    )
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- 1. path confinement


class TestPathConfinement:
    def test_decision_slug_with_traversal_is_refused(self, tmp_brain: Path):
        result = draft_decision(tmp_brain, **_decision_kwargs(slug="../../etc/passwd"))
        assert result.written is False
        assert result.path is None
        assert result.rendered == ""
        assert "bad_filename" in _error_codes(result)
        assert list((tmp_brain / "decisions").glob("*passwd*")) == []

    def test_decision_slug_absolute_path_shaped_is_refused(self, tmp_brain: Path):
        result = draft_decision(tmp_brain, **_decision_kwargs(slug="/etc/passwd"))
        assert result.written is False
        assert "bad_filename" in _error_codes(result)

    def test_hypotheses_feature_slug_with_traversal_is_refused(self, tmp_brain: Path):
        result = draft_hypotheses(tmp_brain, feature_slug="../../etc/passwd", title="x", hypotheses=[])
        assert result.written is False
        assert "bad_filename" in _error_codes(result)

    def test_ingestion_bad_collection_is_refused(self, tmp_brain: Path):
        result = draft_ingestion(
            tmp_brain,
            collection="../source",
            slug="leak",
            date="2026-03-01",
            title="x",
            summary="x",
            claims=[],
        )
        assert result.written is False
        assert "bad_filename" in _error_codes(result)

    def test_ingestion_bad_slug_is_refused(self, tmp_brain: Path):
        result = draft_ingestion(
            tmp_brain,
            collection="interviews",
            slug="../../evil",
            date="2026-03-01",
            title="x",
            summary="x",
            claims=[],
        )
        assert result.written is False
        assert "bad_filename" in _error_codes(result)

    def test_symlinked_target_is_refused_and_disk_untouched(self, tmp_brain: Path, tmp_path: Path):
        target = tmp_brain / "decisions" / "2026-03-01-widget-launch.md"
        outside = tmp_path / "outside.md"
        outside.write_text("do not touch", encoding="utf-8")
        target.symlink_to(outside)

        result = draft_decision(tmp_brain, **_decision_kwargs(allow_replace=True))

        assert result.written is False
        assert "path_confinement" in _error_codes(result)
        assert target.is_symlink()
        assert outside.read_text(encoding="utf-8") == "do not touch"


# --------------------------------------------------------------------------- 2. never overwrite decided/refuted


DECIDED_DECISION_TEXT = """# Decision: Was already decided

## Status
decided

## Date
2026-03-01

## Context
Fixture context.

## Options considered
1. A

## Decision
A

## Why
Fixture reasoning.

## Evidence
- e  (chat, no artifact)

## Explicitly NOT doing

## What would reverse this
If usage drops below 10 accounts by 2026-06-01.

## Remaining ambiguities
"""

SUPERSEDED_DECISION_TEXT = DECIDED_DECISION_TEXT.replace("decided", "superseded", 1)

PENDING_DECISION_TEXT = DECIDED_DECISION_TEXT.replace(
    "## Status\ndecided", "## Status\npending"
).replace(
    "If usage drops below 10 accounts by 2026-06-01.", ""
)


class TestNeverOverwriteDecidedOrRefuted:
    def test_decided_decision_refused_even_with_allow_replace(self, tmp_brain: Path):
        target = tmp_brain / "decisions" / "2026-03-01-widget-launch.md"
        target.write_text(DECIDED_DECISION_TEXT, encoding="utf-8")

        result = draft_decision(tmp_brain, **_decision_kwargs(allow_replace=True))

        assert result.written is False
        assert "record_immutable" in _error_codes(result)
        assert target.read_text(encoding="utf-8") == DECIDED_DECISION_TEXT

    def test_superseded_decision_refused_even_with_allow_replace(self, tmp_brain: Path):
        target = tmp_brain / "decisions" / "2026-03-01-widget-launch.md"
        target.write_text(SUPERSEDED_DECISION_TEXT, encoding="utf-8")

        result = draft_decision(tmp_brain, **_decision_kwargs(allow_replace=True))

        assert result.written is False
        assert "record_immutable" in _error_codes(result)
        assert target.read_text(encoding="utf-8") == SUPERSEDED_DECISION_TEXT

    def test_pending_decision_refused_without_explicit_allow_replace(self, tmp_brain: Path):
        target = tmp_brain / "decisions" / "2026-03-01-widget-launch.md"
        target.write_text(PENDING_DECISION_TEXT, encoding="utf-8")

        result = draft_decision(tmp_brain, **_decision_kwargs())  # allow_replace defaults False

        assert result.written is False
        assert "replace_not_allowed" in _error_codes(result)
        assert target.read_text(encoding="utf-8") == PENDING_DECISION_TEXT

    def test_pending_decision_replaced_with_explicit_allow_replace(self, tmp_brain: Path):
        target = tmp_brain / "decisions" / "2026-03-01-widget-launch.md"
        target.write_text(PENDING_DECISION_TEXT, encoding="utf-8")

        result = draft_decision(tmp_brain, **_decision_kwargs(allow_replace=True))

        assert result.written is True
        assert result.path == target
        assert target.read_text(encoding="utf-8") != PENDING_DECISION_TEXT
        assert "Launch the widget" in target.read_text(encoding="utf-8")

    def test_hypotheses_file_with_refuted_status_refused_even_with_allow_replace(self, tmp_brain: Path):
        target = tmp_brain / "hypotheses" / "widget.md"
        target.write_text(
            "# Hypotheses — widget\n\n## Meta\n- Created: 2026-01-01\n\n"
            "## Value risk\n### H-V1: belief\n- **Origin:** proactive\n- **Confidence:** low\n"
            "- **Evidence for:**\n  - e  (chat, no artifact)\n- **Evidence against:**\n"
            "- **Status:** refuted\n- **Open questions / caveats:**\n",
            encoding="utf-8",
        )
        h = HypothesisDraft(risk="value", belief="new belief", origin="proactive", confidence="low")

        result = draft_hypotheses(tmp_brain, feature_slug="widget", title="widget", hypotheses=[h], allow_replace=True)

        assert result.written is False
        assert "record_immutable" in _error_codes(result)

    def test_hypotheses_file_open_only_refused_without_allow_replace_then_allowed_with_it(self, tmp_brain: Path):
        target = tmp_brain / "hypotheses" / "widget.md"
        target.write_text(
            "# Hypotheses — widget\n\n## Meta\n- Created: 2026-01-01\n\n"
            "## Value risk\n### H-V1: belief\n- **Origin:** proactive\n- **Confidence:** low\n"
            "- **Evidence for:**\n  - e  (chat, no artifact)\n- **Evidence against:**\n"
            "- **Status:** open\n- **Open questions / caveats:**\n",
            encoding="utf-8",
        )
        h = HypothesisDraft(risk="value", belief="new belief", origin="proactive", confidence="low")

        refused = draft_hypotheses(tmp_brain, feature_slug="widget", title="widget", hypotheses=[h])
        assert refused.written is False
        assert "replace_not_allowed" in _error_codes(refused)

        allowed = draft_hypotheses(tmp_brain, feature_slug="widget", title="widget", hypotheses=[h], allow_replace=True)
        assert allowed.written is True
        assert "new belief" in target.read_text(encoding="utf-8")

    def test_ingestion_existing_file_always_refused(self, tmp_brain: Path):
        target = tmp_brain / "ingestion" / "interviews" / "2026-03-01-customer.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# Existing\n\n## Source\nno artifact, verbal only\n\n## Date\n2026-03-01\n\n## Synthesis\nold\n", encoding="utf-8")

        result = draft_ingestion(
            tmp_brain,
            collection="interviews",
            slug="customer",
            date="2026-03-01",
            title="New title",
            summary="new summary",
            claims=[("a claim", "(chat, no artifact)")],
        )

        assert result.written is False
        assert "record_immutable" in _error_codes(result)
        assert "old" in target.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- 3. validate before writing


class TestValidateBeforeWriting:
    def test_effect_on_pending_draft_is_refused_and_nothing_written(self, tmp_brain: Path):
        """A drafted decision is always pending, and the schema-level validator
        already rejects a Quantified effect on anything but a decided decision
        (effect_on_undecided). draft_decision deliberately does not duplicate that
        check — it renders the block when asked and lets validate-before-write refuse
        it, which is exactly this test."""
        target = tmp_brain / "decisions" / "2026-03-01-widget-launch.md"
        result = draft_decision(
            tmp_brain,
            **_decision_kwargs(effect={"category": "REV", "year": 2027, "value": 100.0, "unit": "kEUR"}),
        )

        assert result.written is False
        assert result.path is None
        assert "effect_on_undecided" in _error_codes(result)
        assert result.rendered != ""  # it WAS rendered, just refused at the validate step
        assert not target.exists()

    def test_bad_effect_shape_on_pending_draft_is_refused(self, tmp_brain: Path):
        target = tmp_brain / "decisions" / "2026-03-01-widget-launch.md"
        result = draft_decision(
            tmp_brain,
            **_decision_kwargs(effect={"category": "NOPE", "year": 1999, "value": -5, "unit": "USD"}),
        )
        assert result.written is False
        codes = _error_codes(result)
        assert "effect_on_undecided" in codes or "bad_effect" in codes
        assert not target.exists()

    def test_dry_run_never_writes_but_returns_the_same_findings(self, tmp_brain: Path):
        target = tmp_brain / "decisions" / "2026-03-01-widget-launch.md"

        preview = draft_decision(tmp_brain, **_decision_kwargs(dry_run=True))
        assert preview.written is False
        assert preview.path is None
        assert not target.exists()
        assert preview.rendered != ""
        assert _error_codes(preview) == set()

        real = draft_decision(tmp_brain, **_decision_kwargs())
        assert real.written is True
        assert real.rendered == preview.rendered

    def test_no_effect_block_when_effect_omitted(self, tmp_brain: Path):
        result = draft_decision(tmp_brain, **_decision_kwargs())
        assert result.written is True
        assert "## Quantified effect" not in result.rendered


# --------------------------------------------------------------------------- 3b. a draft must be promotable


class TestDraftMustBePromotable:
    """The trap: `missing_reversal` only fires on a `decided` decision, and a draft is
    always `pending`, so the pending-status validate pass alone can never catch a vague
    reversal condition — the draft writes, and the human's later one-line status edit is
    the thing that gets bounced. These tests pin the fix: draft_decision now also runs a
    second validate pass against a copy of the rendered text with status flipped to
    `decided`, and refuses the draft (writing nothing) if that pass errors."""

    def test_measured_case_vague_reversal_if_things_change_is_refused_at_draft_time(self, tmp_brain: Path):
        target = tmp_brain / "decisions" / "2026-03-01-widget-launch.md"

        result = draft_decision(tmp_brain, **_decision_kwargs(reversal="if things change"))

        assert result.written is False
        assert result.path is None
        assert not target.exists()
        codes = _error_codes(result)
        assert "not_promotable" in codes
        message = next(f.message for f in result.findings if f.code == "not_promotable")
        # The finding must read as "this would fail on promotion", not "the pending
        # file is invalid" -- the pending file is not invalid.
        assert "promot" in message.lower()
        assert "missing_reversal" in message
        # It WAS rendered (validate-before-write always renders first); it just never
        # reached disk.
        assert result.rendered != ""
        assert "pending" in result.rendered  # the file that almost got written is still pending

    @pytest.mark.parametrize("vague", ["TBD", "tbd", "unknown", "Unknown", ""])
    def test_other_vague_reversal_forms_are_refused_the_same_way(self, tmp_brain: Path, vague: str):
        target = tmp_brain / "decisions" / "2026-03-01-widget-launch.md"

        result = draft_decision(tmp_brain, **_decision_kwargs(reversal=vague))

        assert result.written is False
        assert not target.exists()
        assert "not_promotable" in _error_codes(result)
        message = next(f.message for f in result.findings if f.code == "not_promotable")
        assert "missing_reversal" in message

    def test_specific_reversal_drafts_and_then_validates_clean_once_promoted(self, tmp_brain: Path):
        """The property the whole fix exists to guarantee, end to end: a draft that
        passes the promotability gate really is promotable -- editing '## Status' by
        hand from pending to decided produces a file with zero error findings."""
        result = draft_decision(
            tmp_brain, **_decision_kwargs(reversal="If fewer than 2 customers use it within 60 days of ship.")
        )
        assert result.written is True
        assert "not_promotable" not in _error_codes(result)

        promoted_text = result.path.read_text(encoding="utf-8").replace(
            "## Status\npending", "## Status\ndecided", 1
        )
        findings = validate_content(promoted_text, path=result.path, brain_root=tmp_brain)
        assert [f for f in findings if f.severity == "error"] == []

    def test_effect_block_on_pending_draft_is_still_caught_by_the_pending_pass_not_the_promotion_gate(
        self, tmp_brain: Path
    ):
        """Direction 1 of the effect-block contradiction: a `## Quantified effect`
        block is invalid on a pending draft (effect_on_undecided) and *valid* once
        promoted to decided -- the two checks disagree about this block. The
        pending-status pass must keep its veto: it refuses first, unconditionally, so
        the promotion gate never even gets a chance to call the block clean and there
        is no way through it."""
        target = tmp_brain / "decisions" / "2026-03-01-widget-launch.md"
        result = draft_decision(
            tmp_brain,
            **_decision_kwargs(effect={"category": "REV", "year": 2027, "value": 100.0, "unit": "kEUR"}),
        )

        assert result.written is False
        assert not target.exists()
        codes = _error_codes(result)
        assert "effect_on_undecided" in codes
        assert "not_promotable" not in codes  # the pending pass refused; promotion pass never ran

    def test_effect_block_would_indeed_be_valid_once_promoted_which_is_why_pending_must_veto_it(
        self, tmp_brain: Path
    ):
        """Direction 2: prove the disagreement is real, by checking the *promoted*
        shape directly (bypassing the writer). A well-formed REV effect on a decided
        decision is clean -- this is exactly why draft_decision cannot let a pending
        draft with an effect block through on the strength of the promotion pass
        alone; effect_on_undecided on the pending pass is what has to do the work."""
        decided_text = (
            "# Decision: Launch the widget\n\n"
            "## Status\ndecided\n\n"
            "## Date\n2026-03-01\n\n"
            "## Context\nCustomers keep asking for a widget.\n\n"
            "## Options considered\n1. Build it\n2. Don't\n\n"
            "## Decision\nBuild it\n\n"
            "## Why\nCustomers asked.\n\n"
            "## Evidence\n- Three customers asked for it  (chat, no artifact)\n\n"
            "## Explicitly NOT doing\n\n"
            "## What would reverse this\n"
            "If fewer than 2 customers use it within 60 days of ship.\n\n"
            "## Remaining ambiguities\n\n"
            "## Quantified effect\n"
            "- category: REV\n- year: 2027\n- value: 100.0\n- unit: kEUR\n"
        )
        target = tmp_brain / "decisions" / "2026-03-01-widget-launch.md"
        findings = validate_content(decided_text, path=target, brain_root=tmp_brain)
        assert [f for f in findings if f.severity == "error"] == []

    def test_hypothesis_promotion_to_supported_finds_nothing_new(self, tmp_brain: Path):
        """Hypotheses get the same mechanism (draft_hypotheses also runs a promoted-
        status pass, with every rendered '- **Status:** open' flipped to 'supported'),
        but none of validate_content's checks read a hypothesis's specific status value
        beyond "is it a valid enum member" -- there is no hypothesis equivalent of
        missing_reversal. So promotion changes nothing observable for hypotheses: a
        draft that passes the pending-status ('open') pass also passes the promoted
        ('supported') pass, and vice versa. Proven directly (bypassing the writer) by
        running validate_content on the same rendered hypotheses text at both statuses
        and comparing the error sets."""
        h = HypothesisDraft(
            risk="value",
            belief="belief text",
            origin="proactive",
            confidence="low",
            evidence_for=[("ok", "(chat, no artifact)")],
        )
        result = draft_hypotheses(tmp_brain, feature_slug="promotion-parity", title="promotion-parity", hypotheses=[h])
        assert result.written is True
        assert "not_promotable" not in _error_codes(result)

        open_text = result.rendered
        supported_text = open_text.replace("- **Status:** open", "- **Status:** supported")
        assert open_text != supported_text  # sanity: the substitution actually did something

        open_findings = validate_content(open_text, path=result.path, brain_root=tmp_brain)
        supported_findings = validate_content(supported_text, path=result.path, brain_root=tmp_brain)

        open_errors = {(f.code, f.message) for f in open_findings if f.severity == "error"}
        supported_errors = {(f.code, f.message) for f in supported_findings if f.severity == "error"}
        assert open_errors == set()
        assert supported_errors == set()

    def test_hypothesis_with_a_defect_is_refused_by_the_pending_pass_before_promotion_is_even_checked(
        self, tmp_brain: Path
    ):
        """A defect validate_content catches regardless of status (placeholder_row --
        an unfilled template row -- doesn't depend on '**Status:**' at all) is still
        refused, and refused by the pending ('open') pass, same as before this change:
        the promotion gate never gets a chance to run, let alone need to. Checked
        directly with validate_content, on both the open and the would-be-supported
        text, to show the same error fires either way -- consistent with there being
        no hypothesis equivalent of missing_reversal."""
        h = HypothesisDraft(
            risk="value",
            belief="belief text",
            origin="proactive",
            confidence="low",
            evidence_for=[("<placeholder claim>", "(chat, no artifact)")],
        )
        result = draft_hypotheses(tmp_brain, feature_slug="promotion-defect", title="promotion-defect", hypotheses=[h])
        assert result.written is False
        assert "placeholder_row" in _error_codes(result)
        assert "not_promotable" not in _error_codes(result)  # pending pass refused first; no need to promote-check

        target = tmp_brain / "hypotheses" / "promotion-defect.md"
        open_text = result.rendered
        supported_text = open_text.replace("- **Status:** open", "- **Status:** supported")
        open_codes = {f.code for f in validate_content(open_text, path=target, brain_root=tmp_brain) if f.severity == "error"}
        supported_codes = {
            f.code for f in validate_content(supported_text, path=target, brain_root=tmp_brain) if f.severity == "error"
        }
        assert open_codes == supported_codes == {"placeholder_row"}


# --------------------------------------------------------------------------- 4. status is not the caller's to choose


class TestStatusNotCallersToChoose:
    def test_decision_status_is_always_pending_regardless_of_body_text(self, tmp_brain: Path):
        result = draft_decision(
            tmp_brain,
            **_decision_kwargs(
                context="Status: decided. We already made this call, trust me.",
                why="## Status\ndecided\n(smuggled heading-shaped text)",
            ),
        )
        assert result.written is True
        parsed = parse_decision_file(result.path)
        assert parsed.status == "pending"

    def test_hypothesis_status_key_in_dict_input_is_ignored(self, tmp_brain: Path):
        result = draft_hypotheses(
            tmp_brain,
            feature_slug="status-test",
            title="status-test",
            hypotheses=[
                {
                    "risk": "value",
                    "belief": "belief text",
                    "origin": "proactive",
                    "confidence": "low",
                    "status": "refuted",  # must be ignored entirely
                }
            ],
        )
        assert result.written is True
        text = result.path.read_text(encoding="utf-8")
        assert "**Status:** open" in text
        assert "**Status:** refuted" not in text


# --------------------------------------------------------------------------- 5. every claim wears a tag


class TestEveryClaimWearsATag:
    def test_decision_evidence_missing_tag_is_refused_before_rendering(self, tmp_brain: Path):
        result = draft_decision(
            tmp_brain, **_decision_kwargs(evidence=[("a claim with no tag at all", "")])
        )
        assert result.written is False
        assert result.rendered == ""
        assert "invalid_provenance_tag" in _error_codes(result)
        assert "a claim with no tag at all" in result.findings[0].message

    def test_decision_not_doing_bad_tag_form_is_refused(self, tmp_brain: Path):
        result = draft_decision(
            tmp_brain, **_decision_kwargs(not_doing=[("something", "(bogus-kind, x, 2026-01-01)")])
        )
        assert result.written is False
        assert result.rendered == ""
        assert "invalid_provenance_tag" in _error_codes(result)

    def test_hypothesis_evidence_against_missing_tag_is_refused(self, tmp_brain: Path):
        h = HypothesisDraft(
            risk="value",
            belief="belief",
            origin="proactive",
            confidence="low",
            evidence_for=[("ok", "(chat, no artifact)")],
            evidence_against=[("bad claim", "not a real tag")],
        )
        result = draft_hypotheses(tmp_brain, feature_slug="tag-test", title="tag-test", hypotheses=[h])
        assert result.written is False
        assert result.rendered == ""
        assert "invalid_provenance_tag" in _error_codes(result)
        assert "bad claim" in result.findings[0].message

    def test_ingestion_claim_missing_tag_is_refused(self, tmp_brain: Path):
        result = draft_ingestion(
            tmp_brain,
            collection="market",
            slug="scan",
            date="2026-03-01",
            title="Market scan",
            summary="summary",
            claims=[("a competitor shipped X", None)],
        )
        assert result.written is False
        assert result.rendered == ""
        assert "invalid_provenance_tag" in _error_codes(result)


# --------------------------------------------------------------------------- integration


class TestIntegration:
    def test_wellformed_decision_indexes_pending_and_decided_effects_ignores_it(
        self, tmp_brain: Path, prov_session
    ):
        result = draft_decision(tmp_brain, **_decision_kwargs())
        assert result.written is True

        tree_findings = validate_tree(tmp_brain)
        assert [f for f in tree_findings if f.severity == "error"] == []

        report = reindex_tree(prov_session, tmp_brain)
        assert result.path.name not in {p.name for p in report.rejected}

        claim = prov_session.query(Claim).filter(
            Claim.kind == ClaimKind.decision, Claim.slug == result.path.stem
        ).one()
        assert claim.status == "pending"

        effects = decided_effects(prov_session)
        assert result.path.stem not in {e.decision_slug for e in effects}

    def test_hypothesis_draft_round_trips_through_parser(self, tmp_brain: Path):
        h = HypothesisDraft(
            risk="usability",
            belief="PMs will actually fill in the tags",
            origin="data-derived (from beta cohort)",
            confidence="high",
            evidence_for=[("Beta cohort tagged every row in week 1", "(intuition, PM, 2026-03-01)")],
            evidence_against=[("One PM left tags blank under deadline pressure", "(industry-knowledge)")],
            open_questions=["Does this hold past week 1?"],
        )
        result = draft_hypotheses(tmp_brain, feature_slug="tag-adoption", title="Tag adoption", hypotheses=[h])
        assert result.written is True

        parsed = parse_hypothesis_file(result.path)
        assert parsed.title == "Hypotheses — Tag adoption"
        assert parsed.evidence_rows["evidence_for"] == (
            "Beta cohort tagged every row in week 1  (intuition, PM, 2026-03-01)",
        )
        assert parsed.evidence_rows["evidence_against"] == (
            "One PM left tags blank under deadline pressure  (industry-knowledge)",
        )
        assert "**Status:** open" in result.path.read_text(encoding="utf-8")

    def test_ingestion_record_cited_by_later_decision_resolves(self, tmp_brain: Path):
        ingestion_result = draft_ingestion(
            tmp_brain,
            collection="interviews",
            slug="customer-x",
            date="2026-03-01",
            title="Customer X interview synthesis",
            summary="Customer X wants faster exports.",
            claims=[("Customer X asked for CSV export within 2 days", "(stakeholder-verbal, Customer X, 2026-03-01)")],
        )
        assert ingestion_result.written is True

        tag = "[ingestion/interviews/2026-03-01-customer-x.md](../ingestion/interviews/2026-03-01-customer-x.md)"
        decision_result = draft_decision(
            tmp_brain,
            **_decision_kwargs(
                slug="cite-ingestion",
                evidence=[("Customer X wants faster exports", tag)],
            ),
        )
        assert decision_result.written is True
        assert _error_codes(decision_result) == set()

        tree_findings = validate_tree(tmp_brain)
        assert [f for f in tree_findings if f.severity == "error"] == []
