import datetime
from pathlib import Path

import pytest

from provenance.tags import (
    Tag,
    TagKind,
    parse_row,
    parse_tags,
    resolve_path,
    strip_code_spans,
)


# --------------------------------------------------------------------------- one-tag-per-kind


def test_ingestion_tag_parses():
    row = "Enterprise churn is driven by missing SSO [ingestion/interviews/acme.md](../ingestion/interviews/acme.md)"
    parsed = parse_row(row)
    assert parsed.ok, parsed.errors
    assert len(parsed.tags) == 1
    tag = parsed.tags[0]
    assert tag.kind is TagKind.ingestion
    assert tag.target_path == "../ingestion/interviews/acme.md"
    assert tag.link_text == "ingestion/interviews/acme.md"
    assert tag.raw == "[ingestion/interviews/acme.md](../ingestion/interviews/acme.md)"
    assert parsed.text == "Enterprise churn is driven by missing SSO"


def test_source_tag_parses():
    row = "Raw transcript excerpt [source/interviews/acme-2026-04-22.txt](../source/interviews/acme-2026-04-22.txt)"
    parsed = parse_row(row)
    assert parsed.ok, parsed.errors
    tag = parsed.tags[0]
    assert tag.kind is TagKind.source
    assert tag.target_path == "../source/interviews/acme-2026-04-22.txt"
    assert tag.link_text == "source/interviews/acme-2026-04-22.txt"


def test_stakeholder_verbal_tag_parses():
    row = "Customer said pricing is too high (stakeholder-verbal, Jane Doe, 2026-04-22)"
    parsed = parse_row(row)
    assert parsed.ok, parsed.errors
    tag = parsed.tags[0]
    assert tag.kind is TagKind.stakeholder_verbal
    assert tag.who == "Jane Doe"
    assert tag.date == datetime.date(2026, 4, 22)
    assert tag.raw == "(stakeholder-verbal, Jane Doe, 2026-04-22)"


def test_intuition_tag_parses():
    row = "PM believes onboarding friction is the top churn driver (intuition, PM, 2026-05-01)"
    parsed = parse_row(row)
    assert parsed.ok, parsed.errors
    tag = parsed.tags[0]
    assert tag.kind is TagKind.intuition
    assert tag.who == "PM"
    assert tag.date == datetime.date(2026, 5, 1)


def test_industry_knowledge_tag_parses():
    row = "SaaS churn benchmarks sit around 5-7% annually (industry-knowledge)"
    parsed = parse_row(row)
    assert parsed.ok, parsed.errors
    tag = parsed.tags[0]
    assert tag.kind is TagKind.industry_knowledge
    assert tag.who is None
    assert tag.date is None


def test_chat_tag_parses():
    row = "Team brainstormed three positioning angles (chat, no artifact)"
    parsed = parse_row(row)
    assert parsed.ok, parsed.errors
    tag = parsed.tags[0]
    assert tag.kind is TagKind.chat


def test_computed_tag_parses():
    row = "Q3 revenue impact is +4.2% (computed, rev-2026-q3-uplift)"
    parsed = parse_row(row)
    assert parsed.ok, parsed.errors
    tag = parsed.tags[0]
    assert tag.kind is TagKind.computed
    assert tag.derivation_key == "rev-2026-q3-uplift"


# --------------------------------------------------------------------------- orphans


def test_zero_tags_is_orphan():
    parsed = parse_row("Customers dislike the new pricing page")
    assert not parsed.ok
    assert any("orphan" in e for e in parsed.errors)
    assert len(parsed.tags) == 0


def test_bare_parenthetical_is_orphan_not_accepted():
    """The most common real failure: a parenthetical that looks like a citation but is
    missing the required keyword must NOT be silently treated as a valid tag."""
    parsed = parse_row("Customer complained about pricing (Acme interview, 2026-04-22)")
    assert not parsed.ok
    assert any("orphan" in e for e in parsed.errors)
    assert len(parsed.tags) == 0


def test_two_tags_rejected():
    row = (
        "Signal from two places (industry-knowledge) and also "
        "(stakeholder-verbal, Jane Doe, 2026-04-22)"
    )
    parsed = parse_row(row)
    assert not parsed.ok
    assert any("multiple" in e or "two" in e for e in parsed.errors)


# --------------------------------------------------------------------------- code spans


def test_inline_code_span_ignored_when_scanning():
    row = "Example tag syntax is `(industry-knowledge)` - not a real citation here"
    parsed = parse_row(row)
    assert not parsed.ok
    assert any("orphan" in e for e in parsed.errors)
    assert len(parsed.tags) == 0


def test_fenced_code_block_ignored_when_scanning():
    row = "See the example below.\n```\n(industry-knowledge)\n```\nThat was just a demo."
    parsed = parse_row(row)
    assert not parsed.ok
    assert any("orphan" in e for e in parsed.errors)


def test_real_tag_survives_alongside_unrelated_code_span():
    row = "Uses the `industry-knowledge` keyword form (industry-knowledge)"
    parsed = parse_row(row)
    assert parsed.ok, parsed.errors
    assert len(parsed.tags) == 1
    assert parsed.tags[0].kind is TagKind.industry_knowledge


# --------------------------------------------------------------------------- dates


def test_invalid_calendar_date_rejected():
    row = "Feature request logged (stakeholder-verbal, Jane Doe, 2026-02-30)"
    parsed = parse_row(row)
    assert not parsed.ok
    assert len(parsed.tags) == 0
    assert any("date" in e for e in parsed.errors)
    # Not silently treated as an orphan: it's a recognized-but-invalid tag.
    assert not any("orphan" in e for e in parsed.errors)


def test_malformed_date_rejected():
    row = "Feature request logged (intuition, PM, not-a-date)"
    parsed = parse_row(row)
    assert not parsed.ok
    assert any("date" in e for e in parsed.errors)


# --------------------------------------------------------------------------- placeholders


@pytest.mark.parametrize("placeholder", ["<claim>", "<not-doing>", "  <claim>  "])
def test_placeholder_row_detected(placeholder):
    parsed = parse_row(placeholder)
    assert not parsed.ok
    assert any("placeholder" in e for e in parsed.errors)
    assert not any("orphan" in e for e in parsed.errors)


# --------------------------------------------------------------------------- case-insensitivity


@pytest.mark.parametrize(
    "row",
    [
        "Benchmarks apply here (INDUSTRY-KNOWLEDGE)",
        "Benchmarks apply here (Industry-Knowledge)",
    ],
)
def test_industry_knowledge_keyword_case_insensitive_but_raw_preserved(row):
    parsed = parse_row(row)
    assert parsed.ok, parsed.errors
    tag = parsed.tags[0]
    assert tag.kind is TagKind.industry_knowledge
    # raw preserves exactly what was written, not a normalized form.
    assert tag.raw in row


def test_chat_keyword_case_insensitive_but_raw_preserved():
    row = "Team riffed on names (Chat, No Artifact)"
    parsed = parse_row(row)
    assert parsed.ok, parsed.errors
    tag = parsed.tags[0]
    assert tag.kind is TagKind.chat
    assert tag.raw == "(Chat, No Artifact)"


def test_stakeholder_verbal_keyword_case_insensitive():
    row = "Pricing feedback (Stakeholder-Verbal, Jane Doe, 2026-04-22)"
    parsed = parse_row(row)
    assert parsed.ok, parsed.errors
    assert parsed.tags[0].kind is TagKind.stakeholder_verbal
    assert parsed.tags[0].who == "Jane Doe"


# --------------------------------------------------------------------------- parse_tags directly


def test_parse_tags_returns_list_of_tag_objects():
    tags = parse_tags("Pricing feedback (industry-knowledge)")
    assert isinstance(tags, list)
    assert len(tags) == 1
    assert isinstance(tags[0], Tag)


def test_parse_tags_empty_when_no_tag_found():
    assert parse_tags("No citation here at all") == []


# --------------------------------------------------------------------------- resolve_path


def test_resolve_path_ingestion_tag_no_disk_access():
    brain_root = Path("/nonexistent-brain-root-for-testing")
    containing_file = brain_root / "decisions" / "2026-04-22-drop-sso.md"
    tag = Tag(
        kind=TagKind.ingestion,
        raw="[ingestion/interviews/acme.md](../ingestion/interviews/acme.md)",
        target_path="../ingestion/interviews/acme.md",
        link_text="ingestion/interviews/acme.md",
    )
    resolved = resolve_path(tag, containing_file=containing_file, brain_root=brain_root)
    assert resolved == brain_root / "ingestion" / "interviews" / "acme.md"
    # Confirm the path genuinely doesn't exist, proving no filesystem access was required
    # to compute it (the function must not raise or block on a missing tree).
    assert not resolved.exists()


def test_resolve_path_source_tag_relative_containing_file():
    brain_root = Path("/nonexistent-brain-root-for-testing")
    containing_file = Path("decisions/2026-04-22-drop-sso.md")  # relative to brain_root
    tag = Tag(
        kind=TagKind.source,
        raw="[source/x.txt](../source/x.txt)",
        target_path="../source/x.txt",
        link_text="source/x.txt",
    )
    resolved = resolve_path(tag, containing_file=containing_file, brain_root=brain_root)
    assert resolved == brain_root / "source" / "x.txt"


def test_resolve_path_non_path_tag_returns_none():
    tag = Tag(kind=TagKind.industry_knowledge, raw="(industry-knowledge)")
    assert resolve_path(tag, containing_file=Path("/a/b.md"), brain_root=Path("/a")) is None


# --------------------------------------------------------------------------- strip_code_spans


def test_strip_code_spans_removes_inline_code():
    assert "industry-knowledge" not in strip_code_spans("a `industry-knowledge` b")


def test_strip_code_spans_removes_fenced_block():
    text = "before\n```\nindustry-knowledge\n```\nafter"
    stripped = strip_code_spans(text)
    assert "industry-knowledge" not in stripped
    assert "before" in stripped and "after" in stripped
