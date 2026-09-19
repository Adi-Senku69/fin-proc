"""Parse decision, hypothesis and other brain files into a structural shape that
``brainkit.validate`` and ``brainkit.ingest`` can reason about.

PLATFORM.md §5 layout: `## `-heading splitting, a leading `# ` title, and evidence
rows pulled from a fixed set of headings/labels — never from anywhere else.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from provenance import ClaimKind, strip_code_spans

_HEADING1_RE = re.compile(r"^#\s+(.*?)\s*$")
_HEADING2_RE = re.compile(r"^##\s+(.*?)\s*$")
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.*\S)\s*$")
_BOLD_LABEL_RE = re.compile(r"^\s*[-*]\s+\*\*Evidence\s+(for|against)\s*:\*\*\s*$", re.IGNORECASE)
_META_FIELD_RE = re.compile(r"^\*{0,2}([A-Za-z][A-Za-z ]*?)\*{0,2}\s*:\s*(.+)$")

# Decision (evidence heading -> EvidenceSection key). "## Evidence" maps to
# "evidence_for" because decisions have no "for/against" split, only Evidence and
# Explicitly-NOT-doing (PLATFORM.md §4 template).
_DECISION_EVIDENCE_HEADINGS = {
    "evidence": "evidence_for",
    "explicitly not doing": "not_doing",
}

# Content shapes used to classify a file that sits outside every modeled collection
# (brainkit.validate's "misplaced_record" / "unmodeled_file" hole-closing — see
# PLATFORM.md §9). Directory tells us the record type inside decisions/, hypotheses/,
# ingestion/ and knowledge/; outside those, shape is the only signal left.
_SHAPE_BOLD_LABEL_RE = re.compile(r"\*\*Evidence\s+(?:for|against)\s*:\*\*", re.IGNORECASE)
_DECISION_SHAPE_HEADINGS = {"evidence", "explicitly not doing"}


@dataclass(frozen=True)
class ParsedSection:
    heading: str
    body: str
    rows: tuple[str, ...]


@dataclass(frozen=True)
class QuantifiedEffectBlock:
    """The parsed ``## Quantified effect`` block (PLATFORM.md §7.1), before validation.

    ``category``/``unit`` are kept exactly as written (stripped, not case-normalized);
    ``year``/``value`` are ``None`` when the key is missing or its value doesn't parse as
    the expected type - a malformed value never raises here, it lands in ``raw`` verbatim
    and ``brainkit.validate`` reports it as a precise ``bad_effect`` finding.
    """

    category: str
    year: int | None
    value: float | None
    unit: str
    raw: dict[str, str]


@dataclass(frozen=True)
class ParsedFile:
    path: Path
    kind: ClaimKind | None
    title: str | None
    status: str | None
    date: str | None
    sections: tuple[ParsedSection, ...]
    evidence_rows: dict[str, tuple[str, ...]] = field(default_factory=dict)
    body_sha256: str = ""
    errors: tuple[str, ...] = field(default_factory=tuple)
    # Decisions only (PLATFORM.md §7.1); None when the file carries no
    # "## Quantified effect" block, or is not a decision file at all.
    effect: QuantifiedEffectBlock | None = None
    # The indexed form of "## What would reverse this" (PLATFORM.md §4.4),
    # whitespace-normalised. None when the section is absent, empty, or the file is
    # not a decision file at all - the file remains the source of truth.
    reversal_condition: str | None = None


def _read(path: Path) -> tuple[str, str]:
    """Return (utf-8 text, sha256 hex digest of the raw bytes as they sit on disk)."""
    raw_bytes = path.read_bytes()
    return raw_bytes.decode("utf-8"), hashlib.sha256(raw_bytes).hexdigest()


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _split_sections(text: str) -> tuple[str | None, tuple[ParsedSection, ...]]:
    """Split on `## ` headings. A leading `# ` line (before the first `## `) is the
    title and is not itself a section. Code spans are stripped first so an
    illustrative fenced example inside a body never masquerades as a real heading
    or a real evidence row."""
    lines = strip_code_spans(text).splitlines()

    title: str | None = None
    start = 0
    for i, line in enumerate(lines):
        m1 = _HEADING1_RE.match(line)
        if m1:
            title = m1.group(1).strip()
            start = i + 1
            break
        if _HEADING2_RE.match(line):
            start = i
            break

    sections: list[ParsedSection] = []
    heading: str | None = None
    body_lines: list[str] = []

    def flush() -> None:
        if heading is None:
            return
        body = "\n".join(body_lines).strip("\n")
        rows = _extract_rows(body_lines)
        sections.append(ParsedSection(heading=heading, body=body, rows=rows))

    for line in lines[start:]:
        m = _HEADING2_RE.match(line)
        if m:
            flush()
            heading = m.group(1).strip()
            body_lines = []
            continue
        if heading is not None:
            body_lines.append(line)
    flush()
    return title, tuple(sections)


def _extract_rows(body_lines: list[str]) -> tuple[str, ...]:
    """Bullet rows, with soft-wrapped continuation lines (no leading `-`/`*`, part of
    the same list item) folded back into the bullet they continue. A blank line or a
    new bullet ends the current one."""
    rows: list[str] = []
    current: list[str] | None = None
    for line in body_lines:
        bm = _BULLET_RE.match(line)
        if bm:
            if current is not None:
                rows.append(" ".join(current).strip())
            current = [bm.group(1).strip()]
            continue
        if line.strip() == "":
            if current is not None:
                rows.append(" ".join(current).strip())
                current = None
            continue
        if current is not None:
            current.append(line.strip())
    if current is not None:
        rows.append(" ".join(current).strip())
    return tuple(rows)


def _section_body(sections: tuple[ParsedSection, ...], heading: str) -> str | None:
    target = heading.strip().lower()
    for sec in sections:
        if sec.heading.strip().lower() == target:
            return sec.body
    return None


def _first_nonempty_line(text: str | None) -> str | None:
    if not text:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("<!--"):
            return stripped
    return None


def _bold_evidence_rows(text: str) -> dict[str, tuple[str, ...]]:
    """Pull the bullets under every `**Evidence for:**` / `**Evidence against:**`
    label in the file, wherever they occur (hypotheses nest them under per-hypothesis
    `### H-*` headings). A row is anything more indented than the label line;
    anything at the label's indent or shallower ends that list."""
    lines = strip_code_spans(text).splitlines()
    collected: dict[str, list[str]] = {"evidence_for": [], "evidence_against": []}
    i, n = 0, len(lines)
    while i < n:
        m = _BOLD_LABEL_RE.match(lines[i])
        if not m:
            i += 1
            continue
        key = "evidence_for" if m.group(1).lower() == "for" else "evidence_against"
        label_indent = _indent(lines[i])
        i += 1
        current: list[str] | None = None
        while i < n:
            line = lines[i]
            if line.strip() == "":
                i += 1
                continue
            if _indent(line) <= label_indent:
                break
            bm = _BULLET_RE.match(line)
            if bm:
                if current is not None:
                    collected[key].append(" ".join(current).strip())
                current = [bm.group(1).strip()]
            elif current is not None:
                current.append(line.strip())
            i += 1
        if current is not None:
            collected[key].append(" ".join(current).strip())
    return {k: tuple(v) for k, v in collected.items()}


def _meta_fields(sections: tuple[ParsedSection, ...]) -> dict[str, str]:
    body = _section_body(sections, "Meta")
    if body is None:
        return {}
    out: dict[str, str] = {}
    for line in body.splitlines():
        bm = _BULLET_RE.match(line)
        candidate = bm.group(1).strip() if bm else line.strip()
        fm = _META_FIELD_RE.match(candidate)
        if fm:
            out[fm.group(1).strip().lower()] = fm.group(2).strip()
    return out


def _parse_effect_block(sections: tuple[ParsedSection, ...]) -> QuantifiedEffectBlock | None:
    """Parse the optional ``## Quantified effect`` block (PLATFORM.md §7.1): a four-bullet
    ``key: value`` list (``category``, ``year``, ``value``, ``unit``). Tolerant of key
    whitespace/case (``_META_FIELD_RE`` strips bold markers too, matching ``_meta_fields``).
    Returns ``None`` when the heading is absent. A malformed ``year``/``value`` never
    raises: it is recorded in ``raw`` as-is and typed as ``None``."""
    body = _section_body(sections, "Quantified effect")
    if body is None:
        return None

    raw: dict[str, str] = {}
    for line in body.splitlines():
        bm = _BULLET_RE.match(line)
        candidate = bm.group(1).strip() if bm else line.strip()
        if not candidate:
            continue
        fm = _META_FIELD_RE.match(candidate)
        if fm:
            raw[fm.group(1).strip().lower()] = fm.group(2).strip()

    category = raw.get("category", "").strip()
    unit = raw.get("unit", "").strip()

    year: int | None
    try:
        year = int(raw["year"].strip()) if "year" in raw and raw["year"].strip() else None
    except ValueError:
        year = None

    value: float | None
    try:
        value = float(raw["value"].strip()) if "value" in raw and raw["value"].strip() else None
    except ValueError:
        value = None

    return QuantifiedEffectBlock(category=category, year=year, value=value, unit=unit, raw=raw)


def _parse_reversal_condition(sections: tuple[ParsedSection, ...]) -> str | None:
    """The indexed form of ``## What would reverse this`` (PLATFORM.md §4.4):
    whitespace-normalised (runs of whitespace, including newlines, collapsed to a
    single space, then stripped). ``None`` when the heading is absent or its body is
    empty/whitespace-only."""
    body = _section_body(sections, "What would reverse this")
    if body is None:
        return None
    normalized = re.sub(r"\s+", " ", body).strip()
    return normalized or None


def parse_decision_file(path: str | Path) -> ParsedFile:
    path = Path(path)
    text, sha = _read(path)
    title, sections = _split_sections(text)

    status = _first_nonempty_line(_section_body(sections, "Status"))
    date = _first_nonempty_line(_section_body(sections, "Date"))
    effect = _parse_effect_block(sections)
    reversal_condition = _parse_reversal_condition(sections)

    evidence_rows: dict[str, tuple[str, ...]] = {}
    for sec in sections:
        key = _DECISION_EVIDENCE_HEADINGS.get(sec.heading.strip().lower())
        if key:
            evidence_rows[key] = sec.rows

    errors = () if title else ("missing leading '# ' title",)
    return ParsedFile(
        path=path,
        kind=ClaimKind.decision,
        title=title,
        status=status,
        date=date,
        sections=sections,
        evidence_rows=evidence_rows,
        body_sha256=sha,
        errors=errors,
        effect=effect,
        reversal_condition=reversal_condition,
    )


def parse_hypothesis_file(path: str | Path) -> ParsedFile:
    path = Path(path)
    text, sha = _read(path)
    title, sections = _split_sections(text)

    evidence_rows = _bold_evidence_rows(text)
    meta = _meta_fields(sections)
    date = meta.get("last updated") or meta.get("created")

    errors = () if title else ("missing leading '# ' title",)
    return ParsedFile(
        path=path,
        kind=ClaimKind.hypothesis,
        title=title,
        status=None,  # PLATFORM.md's hypothesis schema carries status per-hypothesis, not file-level
        date=date,
        sections=sections,
        evidence_rows=evidence_rows,
        body_sha256=sha,
        errors=errors,
    )


def _parse_generic_file(path: Path) -> ParsedFile:
    """ingestion/, knowledge/, source/, _examples/ and top-level _SCHEMA.md files
    that don't sit under decisions/ or hypotheses/. These carry no evidence-heading
    contract of their own in P1."""
    text, sha = _read(path)
    title, sections = _split_sections(text)
    parts = set(path.parts)
    if "ingestion" in parts:
        kind: ClaimKind | None = ClaimKind.ingestion
    elif "knowledge" in parts:
        kind = ClaimKind.knowledge
    else:
        kind = None  # source/, _examples/ (outside decisions|hypotheses), loose files

    errors = () if title else ("missing leading '# ' title",)
    return ParsedFile(
        path=path,
        kind=kind,
        title=title,
        status=None,
        date=None,
        sections=sections,
        evidence_rows={},
        body_sha256=sha,
        errors=errors,
    )


def sniff_record_shape(text: str) -> ClaimKind | None:
    """Content-based classification for a file whose directory does not identify
    its record type (brainkit.validate calls this only outside decisions/,
    hypotheses/, ingestion/ and knowledge/ — see PLATFORM.md §9).

    Decision-shaped: has a `## Evidence` or `## Explicitly NOT doing` heading.
    Hypothesis-shaped: has a `**Evidence for:**` or `**Evidence against:**` bold
    label. Fenced and inline code spans are stripped first, via
    ``provenance.strip_code_spans``, so a documentation example inside a code block
    can never trigger this. Returns ``None`` if the text matches neither shape.
    """
    clean = strip_code_spans(text)
    for line in clean.splitlines():
        m = _HEADING2_RE.match(line)
        if m and m.group(1).strip().lower() in _DECISION_SHAPE_HEADINGS:
            return ClaimKind.decision
    if _SHAPE_BOLD_LABEL_RE.search(clean):
        return ClaimKind.hypothesis
    return None


def parse_brain_file(path: str | Path) -> ParsedFile:
    """Dispatch on which collection ``path`` sits in."""
    path = Path(path)
    parts = set(path.parts)
    if "decisions" in parts:
        return parse_decision_file(path)
    if "hypotheses" in parts:
        return parse_hypothesis_file(path)
    return _parse_generic_file(path)
