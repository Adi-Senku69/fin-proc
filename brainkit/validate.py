"""Structural validation of brain files (PLATFORM.md §9 — "the schema is the
backstop"). Runs at write time (via the hook) and again at ingest time; a file with
an error-level finding under ``strict`` ingestion is never turned into a Claim row.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from provenance import ClaimKind, TagKind, parse_decision_status, parse_hypothesis_status, parse_row, parse_tags, resolve_path, strip_code_spans

from brainkit.parse import ParsedFile, parse_brain_file, parse_decision_file, parse_hypothesis_file, sniff_record_shape

Severity = Literal["error", "warning"]

_PLACEHOLDER_RE = re.compile(r"<[A-Za-z][A-Za-z0-9 _/-]*>")
_VAGUE_REVERSAL_PHRASES = ("if things change", "tbd", "unknown")
_DECISION_FILENAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9]+(?:-[a-z0-9]+)*$")
_HYPOTHESIS_STATUS_LINE_RE = re.compile(r"^\s*[-]\s+Status\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)

REQUIRED_DECISION_HEADINGS = (
    "Status",
    "Date",
    "Context",
    "Options considered",
    "Decision",
    "Why",
    "Evidence",
    "Explicitly NOT doing",
    "What would reverse this",
    "Remaining ambiguities",
)
REQUIRED_HYPOTHESIS_HEADINGS = ("Meta",)

PATH_TAG_KINDS = (TagKind.ingestion, TagKind.source)

# Directories whose record type is identified by location alone (PLATFORM.md §5).
# A file outside all four gets no free pass: it is classified by content shape
# instead (rule 2/3/4 of the validation-hole fix below).
MODELED_COLLECTIONS = ("decisions", "hypotheses", "ingestion", "knowledge")

_SHAPE_TARGET_DIR = {
    ClaimKind.decision: "decisions/",
    ClaimKind.hypothesis: "hypotheses/",
}


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int | None
    code: str
    message: str
    severity: Severity


def _is_exempt(path: Path, brain_root: Path) -> bool:
    """`_SCHEMA.md` files (they are templates, full of placeholder rows) and
    anything under `_examples/` (deliberate fixtures) never fail the "does the tree
    validate clean" bar — their findings are reported at warning severity at most."""
    if path.name == "_SCHEMA.md":
        return True
    try:
        rel = path.resolve().relative_to(brain_root.resolve())
    except ValueError:
        rel = path
    return rel.parts[:1] == ("_examples",)


def _is_placeholder_row(row: str) -> bool:
    return bool(_PLACEHOLDER_RE.search(row))


def _collection_of(path: Path, brain_root: Path) -> str | None:
    """The top-level modeled collection ``path`` sits directly under, relative to
    ``brain_root`` — ``None`` for the brain root itself or any unmodeled
    subdirectory (``source/`` included). Location, not the literal string
    "decisions" appearing anywhere in the absolute path, is what counts."""
    try:
        rel = path.resolve().relative_to(brain_root.resolve())
    except ValueError:
        rel = path
    parts = rel.parts
    if len(parts) >= 2 and parts[0] in MODELED_COLLECTIONS:
        return parts[0]
    return None


def validate_file(path: str | Path, *, brain_root: str | Path) -> list[Finding]:
    path = Path(path)
    brain_root = Path(brain_root)
    exempt = _is_exempt(path, brain_root)
    findings: list[Finding] = []

    def add(code: str, message: str, severity: Severity = "error", line: int | None = None) -> None:
        sev: Severity = "warning" if (exempt and severity == "error") else severity
        findings.append(Finding(path=path, line=line, code=code, message=message, severity=sev))

    raw_text = path.read_text(encoding="utf-8")
    clean_text = strip_code_spans(raw_text)

    collection = _collection_of(path, brain_root)
    directory_kind: ClaimKind | None = {
        "decisions": ClaimKind.decision,
        "hypotheses": ClaimKind.hypothesis,
    }.get(collection)

    # --- misplaced_record / unmodeled_file: every .md under brain/ gets validated
    # (PLATFORM.md §9). decisions/ and hypotheses/ pin a record type by location, but
    # content can still contradict it (a hypothesis-shaped file dropped in
    # decisions/); everywhere else — the brain root, source/, or any other stray
    # directory — location says nothing at all, so content shape is the only
    # signal. ingestion/ and knowledge/ carry no evidence-heading contract of their
    # own in P1 (brainkit.parse._parse_generic_file) and are left exactly as before.
    shape: ClaimKind | None = None
    if collection not in ("ingestion", "knowledge"):
        shape = sniff_record_shape(raw_text)

    effective_kind = directory_kind
    if shape is not None and shape is not directory_kind:
        effective_kind = shape
        current_where = f"{collection}/" if collection else "outside any modeled collection"
        add(
            "misplaced_record",
            f"{path.name} is {shape.value}-shaped but sits in {current_where}, not "
            f"in {_SHAPE_TARGET_DIR[shape]} where {shape.value} records belong",
        )
    elif directory_kind is None and shape is None and collection is None:
        add(
            "unmodeled_file",
            f"{path.name} is outside every modeled brain/ collection "
            f"(decisions/, hypotheses/, ingestion/, knowledge/) and does not "
            f"match a known record shape",
            severity="warning",
        )

    is_decision = effective_kind is ClaimKind.decision
    is_hypothesis = effective_kind is ClaimKind.hypothesis

    if is_decision:
        parsed: ParsedFile = parse_decision_file(path)
    elif is_hypothesis:
        parsed = parse_hypothesis_file(path)
    else:
        parsed = parse_brain_file(path)

    # --- orphan_evidence: every evidence bullet carries exactly one provenance tag.
    all_placeholder_rows: set[str] = set()
    for rows in parsed.evidence_rows.values():
        for row in rows:
            if _is_placeholder_row(row):
                all_placeholder_rows.add(row)
                continue
            row_parse = parse_row(row)
            if not row_parse.ok:
                add("orphan_evidence", f"evidence row without exactly one provenance tag: {row[:100]!r}")

    # --- placeholder_row: an unfilled template row, wherever it appears.
    for sec in parsed.sections:
        for row in sec.rows:
            if _is_placeholder_row(row):
                all_placeholder_rows.add(row)
    for row in all_placeholder_rows:
        add("placeholder_row", f"unfilled template row: {row[:100]!r}", severity="warning" if exempt else "error")

    # --- unresolved_link: every path-typed tag in the file resolves on disk.
    for tag in parse_tags(clean_text):
        if tag.kind not in PATH_TAG_KINDS:
            continue
        resolved = resolve_path(tag, containing_file=path.resolve(), brain_root=brain_root.resolve())
        if resolved is None or not resolved.exists():
            add("unresolved_link", f"{tag.kind.value} tag target does not resolve: {tag.target_path!r}")

    # --- bad_status
    if is_decision:
        if not parsed.status:
            add("bad_status", "decision status missing")
        else:
            try:
                parse_decision_status(parsed.status)
            except ValueError as exc:
                add("bad_status", f"decision status invalid: {exc}")
    if is_hypothesis:
        status_lines = _HYPOTHESIS_STATUS_LINE_RE.findall(clean_text.replace("*", ""))
        if not status_lines:
            add("bad_status", "no per-hypothesis '**Status:**' line found")
        for value in status_lines:
            try:
                parse_hypothesis_status(value.strip())
            except ValueError as exc:
                add("bad_status", f"hypothesis status invalid: {exc}")

    # --- missing_reversal: a decided decision names a real, checkable condition.
    if is_decision and (parsed.status or "").strip().lower() == "decided":
        reversal = next(
            (s.body.strip() for s in parsed.sections if s.heading.strip().lower() == "what would reverse this"),
            "",
        )
        normalized = reversal.lower()
        if not reversal or any(phrase in normalized for phrase in _VAGUE_REVERSAL_PHRASES):
            add("missing_reversal", "decided decision has no specific, observable reversal condition")

    # --- missing_section
    present = {s.heading.strip().lower() for s in parsed.sections}
    if is_decision:
        for heading in REQUIRED_DECISION_HEADINGS:
            if heading.lower() not in present:
                add("missing_section", f"required heading missing: ## {heading}")
    if is_hypothesis:
        for heading in REQUIRED_HYPOTHESIS_HEADINGS:
            if heading.lower() not in present:
                add("missing_section", f"required heading missing: ## {heading}")

    # --- bad_filename. _SCHEMA.md never matches the record pattern by construction;
    # it is still checked (for uniformity) but relies entirely on the exemption above.
    if is_decision and path.suffix == ".md":
        if not _DECISION_FILENAME_RE.match(path.stem):
            add("bad_filename", f"decision filename does not match YYYY-MM-DD-<slug>.md: {path.name!r}")

    return findings


def validate_tree(brain_root: str | Path) -> list[Finding]:
    brain_root = Path(brain_root)
    findings: list[Finding] = []
    for md_path in sorted(brain_root.rglob("*.md")):
        findings.extend(validate_file(md_path, brain_root=brain_root))
    return findings
