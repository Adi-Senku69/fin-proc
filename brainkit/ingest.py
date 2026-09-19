"""Ingest the ``brain/`` markdown tree into the derived index (PLATFORM.md §3, §6).

The tree is authoritative; this module only ever produces a rebuildable cache of it.
Validation runs first (``brainkit.validate``) and, under ``strict=True``, a file
carrying any error-level finding is never turned into a row — an unsourced claim can
never become a row (PLATFORM.md §4.2, §9.2).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path
from typing import Callable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from provenance import (
    Claim,
    ClaimKind,
    ClaimLink,
    Evidence,
    EvidenceSection,
    LinkRelation,
    TagKind,
    parse_row,
    resolve_path,
    strip_code_spans,
)

from brainkit.parse import ParsedFile, parse_brain_file
from brainkit.validate import Finding, _effect_problems, _is_placeholder_row, validate_file

# Collections that actually become Claim rows. `source/` is raw material and is never
# rewritten or ingested; `_examples/` is fixture space; `_SCHEMA.md` is a template.
INGESTIBLE_COLLECTIONS = ("decisions", "hypotheses", "ingestion", "knowledge")

DerivationLookup = Callable[[str], "int | None"]


@dataclass(frozen=True)
class IngestReport:
    files_seen: int
    ingested: int
    skipped_unchanged: int
    rejected: tuple[Path, ...]
    findings: tuple[Finding, ...] = field(default_factory=tuple)


def _iter_candidate_files(brain_root: Path):
    for collection in INGESTIBLE_COLLECTIONS:
        root = brain_root / collection
        if not root.is_dir():
            continue
        for md_path in sorted(root.rglob("*.md")):
            if md_path.name == "_SCHEMA.md":
                continue
            yield md_path


def _claim_kind_for(parsed: ParsedFile, path: Path) -> ClaimKind:
    if parsed.kind is not None:
        return parsed.kind
    return ClaimKind.knowledge


def _parse_date_or_none(text: str | None) -> _date | None:
    if not text:
        return None
    try:
        return _date.fromisoformat(text.strip())
    except ValueError:
        return None


def _resolve_relative(containing_file: Path, target: str) -> Path:
    """Lexical resolution for an ordinary markdown link (not a provenance tag) —
    used only for supersedes-link detection between decision files."""
    return (containing_file.parent / target).resolve()


def _get_or_create_computed_claim(session: Session, derivation_key: str, derivation_id: int | None) -> Claim:
    """A `(computed, <key>)` tag doesn't cite a file — it cites a specific finance
    derivation. We index that as a lightweight, file-less Claim of kind `computed`
    (``Claim.path`` stays NULL) so the decision's `quantifies` link has a real claim
    on both ends, and `Claim.derivation_id` carries the resolved figure."""
    slug = f"computed:{derivation_key}"
    existing = session.execute(select(Claim).where(Claim.kind == ClaimKind.computed, Claim.slug == slug)).scalar_one_or_none()
    sha = hashlib.sha256(slug.encode("utf-8")).hexdigest()
    if existing is not None:
        existing.derivation_id = derivation_id
        return existing
    claim = Claim(
        kind=ClaimKind.computed,
        slug=slug,
        path=None,
        title=f"computed figure: {derivation_key}",
        status=None,
        date=None,
        body_sha256=sha,
        derivation_id=derivation_id,
    )
    session.add(claim)
    session.flush()
    return claim


def _resolve_evidence_tag(
    tag, containing_path: Path, brain_root: Path, derivation_lookup: DerivationLookup | None
) -> tuple[bool, int | None]:
    """Returns (resolved, target_derivation_id)."""
    if tag.kind in (TagKind.ingestion, TagKind.source):
        target = resolve_path(tag, containing_file=containing_path.resolve(), brain_root=brain_root.resolve())
        return (target is not None and target.exists()), None
    if tag.kind is TagKind.computed:
        derivation_id = derivation_lookup(tag.derivation_key) if derivation_lookup and tag.derivation_key else None
        return derivation_id is not None, derivation_id
    # stakeholder-verbal / intuition / industry-knowledge / chat: self-contained
    # assertions with no external target to resolve — nothing is "unresolved" here.
    return True, None


def _write_claim_body(
    session: Session,
    claim: Claim,
    parsed: ParsedFile,
    *,
    brain_root: Path,
    derivation_lookup: DerivationLookup | None,
) -> None:
    """(Re)write a claim's Evidence and outgoing ClaimLink rows to match its current
    parsed content. Safe to call on either a brand-new or a just-updated Claim."""
    session.execute(delete(Evidence).where(Evidence.claim_id == claim.id))
    session.execute(delete(ClaimLink).where(ClaimLink.from_claim_id == claim.id))

    linked_computed_claims: set[int] = set()
    for section_key, rows in parsed.evidence_rows.items():
        section = EvidenceSection(section_key)
        for row in rows:
            if _is_placeholder_row(row):
                continue
            row_parse = parse_row(row)
            if not row_parse.ok:
                # strict=True already rejected the file before we get here; under
                # strict=False this is defensive, and an unsourced row is simply
                # skipped rather than stored — a claim without provenance never
                # becomes a row (PLATFORM.md §4.2).
                continue
            tag = row_parse.tags[0]
            resolved, target_derivation_id = _resolve_evidence_tag(tag, parsed.path, brain_root, derivation_lookup)
            session.add(
                Evidence(
                    claim_id=claim.id,
                    section=section,
                    text=row,
                    tag_kind=tag.kind,
                    tag_raw=tag.raw,
                    target_path=tag.target_path,
                    target_claim_id=None,
                    target_derivation_id=target_derivation_id,
                    resolved=resolved,
                )
            )
            if tag.kind is TagKind.computed and tag.derivation_key:
                computed_claim = _get_or_create_computed_claim(session, tag.derivation_key, target_derivation_id)
                if computed_claim.id not in linked_computed_claims:
                    linked_computed_claims.add(computed_claim.id)
                    session.add(ClaimLink(from_claim_id=claim.id, to_claim_id=computed_claim.id, relation=LinkRelation.quantifies))

    # supersedes: any markdown link from a decision to another file under decisions/.
    if "decisions" in set(parsed.path.parts):
        raw_text = strip_code_spans(parsed.path.read_text(encoding="utf-8"))
        decisions_dir = (brain_root / "decisions").resolve()
        linked_predecessors: set[int] = set()
        for tag_like in _extract_all_links(raw_text):
            target = _resolve_relative(parsed.path, tag_like)
            if not target.exists() or target == parsed.path.resolve():
                continue
            try:
                target.relative_to(decisions_dir)
            except ValueError:
                continue
            target_path_str = str(target.relative_to(brain_root.resolve()).as_posix())
            target_claim = session.execute(select(Claim).where(Claim.path == target_path_str)).scalar_one_or_none()
            if target_claim is not None and target_claim.id not in linked_predecessors:
                linked_predecessors.add(target_claim.id)
                session.add(ClaimLink(from_claim_id=claim.id, to_claim_id=target_claim.id, relation=LinkRelation.supersedes))


def _extract_all_links(text: str) -> list[str]:
    out = []
    for m in re.finditer(r"\[([^\]]*)\]\(([^)]+)\)", text):
        target = m.group(2).split("#", 1)[0].strip()
        if target and not target.startswith(("http://", "https://", "mailto:")):
            out.append(target)
    return out


def ingest_tree(
    session: Session,
    brain_root: str | Path,
    *,
    strict: bool = True,
    derivation_lookup: DerivationLookup | None = None,
) -> IngestReport:
    brain_root = Path(brain_root)

    files_seen = 0
    ingested_count = 0
    skipped_unchanged = 0
    rejected: list[Path] = []
    all_findings: list[Finding] = []

    for path in _iter_candidate_files(brain_root):
        files_seen += 1
        findings = validate_file(path, brain_root=brain_root)
        all_findings.extend(findings)
        has_error = any(f.severity == "error" for f in findings)

        if strict and has_error:
            rejected.append(path)
            continue

        parsed = parse_brain_file(path)
        path_str = str(path.resolve().relative_to(brain_root.resolve()).as_posix())

        existing = session.execute(select(Claim).where(Claim.path == path_str)).scalar_one_or_none()
        if existing is not None and existing.body_sha256 == parsed.body_sha256:
            skipped_unchanged += 1
            continue

        claim_date = _parse_date_or_none(parsed.date)
        # Quantified effect (PLATFORM.md §7.1): indexed only when the parsed block is
        # well-formed. A file carrying a malformed effect never reaches here under
        # strict=True (validate_file already flagged it error-level and it was
        # rejected above); under strict=False this stays defensive and simply
        # indexes nothing rather than a half-parsed value.
        effect_json = None
        is_decided = (parsed.status or "").strip().lower() == "decided"
        if parsed.effect is not None and is_decided and not _effect_problems(parsed.effect):
            e = parsed.effect
            effect_json = {"category": e.category.strip(), "year": e.year, "value": e.value, "unit": e.unit.strip()}

        if existing is not None:
            claim = existing
            claim.title = parsed.title or path.stem
            claim.status = parsed.status
            claim.date = claim_date
            claim.body_sha256 = parsed.body_sha256
            claim.effect_json = effect_json
        else:
            claim = Claim(
                kind=_claim_kind_for(parsed, path),
                slug=path.stem,
                path=path_str,
                title=parsed.title or path.stem,
                status=parsed.status,
                date=claim_date,
                body_sha256=parsed.body_sha256,
                effect_json=effect_json,
            )
            session.add(claim)
            session.flush()

        _write_claim_body(session, claim, parsed, brain_root=brain_root, derivation_lookup=derivation_lookup)
        ingested_count += 1

    session.commit()
    return IngestReport(
        files_seen=files_seen,
        ingested=ingested_count,
        skipped_unchanged=skipped_unchanged,
        rejected=tuple(rejected),
        findings=tuple(all_findings),
    )


def rebuild(session: Session, brain_root: str | Path) -> IngestReport:
    """Wipe the index tables, then ingest the tree fresh. The tree is the source of
    truth (PLATFORM.md §3); the index is always safe to throw away and recompute."""
    session.execute(delete(ClaimLink))
    session.execute(delete(Evidence))
    session.execute(delete(Claim))
    session.commit()
    return ingest_tree(session, brain_root, strict=True)
