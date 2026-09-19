"""The write substrate for the brain (PLATFORM.md §12, phase B1).

Nothing in the codebase writes a ``brain/`` markdown file except this module. Every
record that exists today was hand-authored; the whole point of this module is that a
record an agent drafts is held to exactly the same bar as one a person writes by hand
— checked *before* it touches disk, never after.

PLATFORM.md §12.3 lists five hard limits on the writer. Each is enforced here, in this
order of execution (the numbering below matches §12.3, not call order):

  1. Path confinement — ``_slug_pattern``/``_validate_date``/``_validate_collection``
     restrict every caller-supplied path fragment to a closed character set before it
     ever reaches a filesystem path, and ``_confine`` re-checks the assembled target
     (containment in ``brain_root`` and its collection dir, no symlink) as a backstop.
  2. Never overwrite a decided/superseded/refuted record — ``_check_decision_overwrite``
     / ``_check_hypothesis_overwrite`` parse the existing file (if any) and refuse
     unconditionally on those statuses; a ``pending``/``open`` file may be replaced only
     with ``allow_replace=True``.
  3. Validate before writing — every draft function renders to a string first, then
     runs ``brainkit.validate.validate_content`` against the *rendered* text at the
     real target path, and writes only when there is no error-level finding. A
     rejected draft never touches disk.
  4. Status is not the caller's to choose — there is no status parameter on
     ``draft_decision`` (it always renders ``pending``) or on a hypothesis item's
     dict/``HypothesisDraft`` (any ``status`` key, if present, is never read; the
     renderer always writes ``open``).
  5. Every claim wears a tag — ``_check_tag_pairs`` runs ``provenance.parse_row`` on
     each ``(text, tag)`` pair's tag *before* anything is rendered, and refuses with a
     message naming the offending claim if the tag is absent or invalid.

Effect-on-a-draft (PLATFORM.md §7.1): a drafted decision is always ``pending``, and the
schema is explicit that a ``## Quantified effect`` block is valid only on a ``decided``
decision. Rather than duplicate that rule here, ``draft_decision`` renders the block
whenever ``effect`` is given and lets step 3 (validate-before-write) refuse it — the
existing validator already raises ``effect_on_undecided`` for exactly this shape, so a
caller who tries to hand a draft a quantified effect gets a clear, single-source-of-truth
refusal instead of two different codes for the same fact.

A draft must be promotable (closing the "dead on arrival" trap: a vague reversal
condition, or anything else that only errors once a record is no longer ``pending``/
``open``, was accepted at draft time and then bounced back at the human's desk the
moment they made the one-line status edit the whole design asks them to make).
``missing_reversal`` only fires on a ``decided`` decision, so a pending-status validate
pass alone can never catch it. So after the pending-status pass finds no error, both
``draft_decision`` and ``draft_hypotheses`` run **a second, additional** validate pass
against a copy of the same rendered text with its status flipped to the value a human
promotion would set it to (``decided`` / ``supported`` — ``provenance.lifecycle``'s own
spelling, not a guess). Any error from that second pass refuses the draft too, wrapped
so the message says the draft would fail *when promoted*, not that the pending file on
disk is wrong (it isn't — nothing about the pending-status pass changes). This is
additive, never a replacement: the pending-status pass still runs first and still
refuses on its own terms.

This also settles the one place the two passes would otherwise disagree.
``effect_on_undecided`` fires on the pending-status pass for *any* ``## Quantified
effect`` block, well-formed or not, because a drafted decision is never anything but
``pending``. That means ``draft_decision`` already refuses before the promoted-status
pass ever runs whenever ``effect`` is given — the same block that pending-status
validation calls an error is one the promoted-status pass would call clean, but the
promoted-status pass never gets a chance to say so, because the pending-status pass
refuses first and unconditionally. There is no way to reach the promoted-status pass
with an effect block attached (the renderer only emits one when ``effect`` is
given, and giving one always trips ``effect_on_undecided``), so there is no gap for a
caller to smuggle an effect through: the existing "an effect on a draft is always
refused" behavior is unchanged, and it is what keeps the two passes from contradicting
each other in practice. Ingestion records carry no lifecycle status (PLATFORM.md
§12.4: "n/a, a record not a judgement"), so ``draft_ingestion`` has no promoted-status
pass at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path
from typing import Sequence

from provenance import DecisionStatus, HypothesisStatus, TagKind, parse_row, strip_code_spans

from brainkit.parse import parse_decision_file
from brainkit.validate import Finding, validate_content

__all__ = [
    "DraftResult",
    "HypothesisDraft",
    "draft_decision",
    "draft_hypotheses",
    "draft_ingestion",
]

# --------------------------------------------------------------------------- patterns

# Lowercase-alnum-and-hyphen only: no '.', no '/', no leading/trailing hyphen. This is
# the entire defense against a caller-supplied slug/feature-slug escaping its directory
# via '..' or an absolute path — neither character is in the allowed set, so the same
# check that rejects a malformed slug also rejects a path-escape attempt.
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HYPOTHESIS_STATUS_LINE_RE = re.compile(r"^\s*[-]\s+Status\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)

INGESTION_COLLECTIONS = ("interviews", "meetings", "market", "adhoc")

# Risk area -> (heading text, H-code letter), in the _SCHEMA.md's fixed order.
RISK_HEADINGS: dict[str, tuple[str, str]] = {
    "value": ("Value risk", "V"),
    "usability": ("Usability risk", "U"),
    "feasibility": ("Feasibility risk", "F"),
    "viability": ("Viability risk", "B"),
    "other": ("Other risk", "O"),
}

# Decision statuses that can never be overwritten in place, regardless of allow_replace
# (PLATFORM.md §12.3.2). "refuted" is a hypothesis-only status; the two checks below each
# apply the subset that is meaningful for their own record kind.
_DECISION_HARD_BLOCK = {"decided", "superseded"}
_HYPOTHESIS_HARD_BLOCK = {"refuted", "superseded"}


@dataclass(frozen=True)
class DraftResult:
    """The result of a draft attempt. ``path`` is the real target path when (and only
    when) ``written`` is True; a refused or dry-run draft always gets ``path=None``.
    ``findings`` explains a refusal, or lists warnings on a successful write.
    ``rendered`` is the text that was (or would have been) written — empty when a check
    that runs *before* rendering (path confinement, overwrite protection, a bad tag)
    refused the draft, since in that case nothing was ever rendered at all."""

    path: Path | None
    written: bool
    findings: list[Finding]
    rendered: str


@dataclass(frozen=True)
class HypothesisDraft:
    """One hypothesis for ``draft_hypotheses``. ``risk`` is one of the five
    ``RISK_HEADINGS`` keys. Any ``status`` a caller tries to attach elsewhere is never
    read — a drafted hypothesis is always rendered ``open`` (PLATFORM.md §12.3.4)."""

    risk: str
    belief: str
    origin: str
    confidence: str
    evidence_for: Sequence[tuple[str, str]] = ()
    evidence_against: Sequence[tuple[str, str]] = ()
    open_questions: Sequence[str] = field(default_factory=tuple)


def _coerce_hypothesis(item: HypothesisDraft | dict) -> HypothesisDraft:
    if isinstance(item, HypothesisDraft):
        return item
    if isinstance(item, dict):
        # Deliberately never reads item.get("status", ...) — see the class docstring.
        return HypothesisDraft(
            risk=item["risk"],
            belief=item["belief"],
            origin=item.get("origin", "proactive"),
            confidence=item.get("confidence", "medium"),
            evidence_for=tuple(item.get("evidence_for", ())),
            evidence_against=tuple(item.get("evidence_against", ())),
            open_questions=tuple(item.get("open_questions", ())),
        )
    raise TypeError(f"hypothesis item must be a HypothesisDraft or dict, got {type(item)!r}")


# --------------------------------------------------------------------------- refusal helper


def _refuse(target: Path, code: str, message: str) -> DraftResult:
    finding = Finding(path=target, line=None, code=code, message=message, severity="error")
    return DraftResult(path=None, written=False, findings=[finding], rendered="")


# --------------------------------------------------------------------------- input validation


def _validate_slug(value: object, *, field_name: str) -> str | None:
    if not isinstance(value, str) or not _SLUG_RE.match(value):
        return (
            f"{field_name} {value!r} must match ^[a-z0-9]+(-[a-z0-9]+)*$ (lowercase "
            "letters, digits and hyphens only) — this is also what rejects a '..' or "
            "an absolute-path-shaped value"
        )
    return None


def _validate_date(value: object) -> str | None:
    if not isinstance(value, str) or not _DATE_RE.match(value):
        return f"date {value!r} must be a YYYY-MM-DD string"
    try:
        _date.fromisoformat(value)
    except ValueError as exc:
        return f"date {value!r} is not a valid calendar date: {exc}"
    return None


def _validate_collection(value: object) -> str | None:
    if value not in INGESTION_COLLECTIONS:
        return f"collection {value!r} must be one of {INGESTION_COLLECTIONS}"
    return None


def _check_tag_pairs(pairs: Sequence[tuple[str, str]], *, label: str) -> str | None:
    """PLATFORM.md §12.3.5: every (text, tag) pair's tag must be exactly one valid,
    closed-enum provenance tag, checked with the same parser the validator uses,
    *before* anything is rendered. Returns a message naming the offending claim, or
    None if every pair is clean."""
    for i, pair in enumerate(pairs):
        if not (isinstance(pair, (tuple, list)) and len(pair) == 2):
            return f"{label}[{i}] must be a (text, tag) pair, got {pair!r}"
        text, tag = pair
        if not isinstance(tag, str) or not tag.strip():
            return f"{label}[{i}] is missing a provenance tag for claim {text!r}"
        row_parse = parse_row(tag)
        if not row_parse.ok or len(row_parse.tags) != 1:
            reason = "; ".join(row_parse.errors) or "must be exactly one tag from the closed PLATFORM.md §4.1 enum"
            return f"{label}[{i}] has an invalid provenance tag {tag!r} for claim {text!r}: {reason}"
    return None


# --------------------------------------------------------------------------- path confinement


def _confine(candidate: Path, *, brain_root: Path, collection_dir: Path) -> str | None:
    """PLATFORM.md §12.3.1, backstop half: refuse a target that is, or resolves through,
    a symlink, or that (however it was built) doesn't land exactly inside
    ``collection_dir``. ``Path.resolve()`` fully resolves every symlink in the existing
    part of the path and normalizes '..' lexically for the rest, so this also catches a
    traversal that the slug/date character-set checks somehow missed."""
    if candidate.is_symlink():
        return f"refusing to write through a symlink: {candidate}"
    resolved_root = brain_root.resolve()
    resolved_collection = collection_dir.resolve()
    resolved_candidate = candidate.resolve()
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError:
        return f"target path escapes brain_root ({resolved_root}): {candidate}"
    if resolved_candidate.parent != resolved_collection:
        return f"target path escapes its collection directory ({resolved_collection}): {candidate}"
    return None


# --------------------------------------------------------------------------- overwrite protection


def _check_decision_overwrite(target: Path, *, allow_replace: bool) -> str | None:
    if not target.exists():
        return None
    parsed = parse_decision_file(target)
    status = (parsed.status or "").strip().lower()
    if status in _DECISION_HARD_BLOCK:
        return f"refusing to overwrite {target}: status is {status!r}, which is never overwritten"
    if not allow_replace:
        return (
            f"{target} already exists (status={status or 'unknown'}); pass allow_replace=True "
            "to replace a pending record explicitly"
        )
    return None


def _check_hypothesis_overwrite(target: Path, *, allow_replace: bool) -> str | None:
    if not target.exists():
        return None
    text = strip_code_spans(target.read_text(encoding="utf-8")).replace("*", "")
    statuses = {m.group(1).strip().lower() for m in _HYPOTHESIS_STATUS_LINE_RE.finditer(text)}
    hard_blocked = statuses & _HYPOTHESIS_HARD_BLOCK
    if hard_blocked:
        return (
            f"refusing to overwrite {target}: contains hypothesis status(es) "
            f"{sorted(hard_blocked)}, which are never overwritten"
        )
    if not allow_replace:
        return (
            f"{target} already exists (statuses={sorted(statuses) or ['unknown']}); pass "
            "allow_replace=True to replace an open/supported record explicitly"
        )
    return None


def _check_ingestion_overwrite(target: Path) -> str | None:
    """Ingestion records carry no lifecycle status (PLATFORM.md §12.4: "n/a, a record
    not a judgement"), so there is no safe "replace a pending one" case to gate behind
    an allow_replace flag. draft_ingestion refuses unconditionally on an existing file."""
    if target.exists():
        return f"refusing to overwrite an existing ingestion record (no replace path in phase B1): {target}"
    return None


# --------------------------------------------------------------------------- rendering helpers


def _section(heading: str, body: str) -> str:
    return f"## {heading}\n{body}".rstrip("\n")


def _bullet(text: str, tag: str) -> str:
    return f"- {text.strip()}  {tag.strip()}"


def _render_pairs(pairs: Sequence[tuple[str, str]]) -> str:
    return "\n".join(_bullet(t, g) for t, g in pairs)


def _render_nested_pairs(pairs: Sequence[tuple[str, str]], indent: str = "  ") -> str:
    return "\n".join(f"{indent}- {t.strip()}  {g.strip()}" for t, g in pairs)


def _render_options(options: Sequence[str]) -> str:
    return "\n".join(f"{i}. {opt}" for i, opt in enumerate(options, start=1))


def _render_effect(effect: dict) -> list[str]:
    return [f"- {key}: {effect.get(key, '')}" for key in ("category", "year", "value", "unit")]


def _render_decision_text(
    *,
    title: str,
    date: str,
    context: str,
    options: Sequence[str],
    decision: str,
    why: str,
    evidence: Sequence[tuple[str, str]],
    not_doing: Sequence[tuple[str, str]],
    reversal: str | None,
    ambiguities: str | None,
    effect: dict | None,
) -> str:
    parts = [
        f"# Decision: {title}",
        # PLATFORM.md §12.3.4: not a parameter anywhere in this function — always pending.
        _section("Status", "pending"),
        _section("Date", date),
        _section("Context", context.strip()),
        _section("Options considered", _render_options(options)),
        _section("Decision", decision.strip()),
        _section("Why", why.strip()),
        _section("Evidence", _render_pairs(evidence)),
        _section("Explicitly NOT doing", _render_pairs(not_doing)),
        _section("What would reverse this", (reversal or "").strip()),
        _section("Remaining ambiguities", (ambiguities or "").strip()),
    ]
    if effect is not None:
        parts.append(_section("Quantified effect", "\n".join(_render_effect(effect))))
    return "\n\n".join(parts) + "\n"


def _render_hypothesis_block(code: str, idx: int, h: HypothesisDraft) -> str:
    lines = [
        f"### H-{code}{idx}: {h.belief}",
        f"- **Origin:** {h.origin}",
        f"- **Confidence:** {h.confidence}",
        "- **Evidence for:**",
    ]
    ev_for = _render_nested_pairs(h.evidence_for)
    if ev_for:
        lines.append(ev_for)
    lines.append("- **Evidence against:**")
    ev_against = _render_nested_pairs(h.evidence_against)
    if ev_against:
        lines.append(ev_against)
    # PLATFORM.md §12.3.4: hardcoded, never read from the caller's input.
    lines.append("- **Status:** open")
    lines.append("- **Open questions / caveats:**")
    oq = "\n".join(f"  - {q.strip()}" for q in h.open_questions)
    if oq:
        lines.append(oq)
    return "\n".join(lines)


def _render_hypotheses_text(
    *, title: str, feature_slug: str, brain_root: Path, hypotheses: Sequence[HypothesisDraft]
) -> str:
    today = _date.today().isoformat()
    feature_path = brain_root / "knowledge" / "product" / "features" / f"{feature_slug}.md"
    if feature_path.exists():
        feature_line = f"[{feature_slug}](../knowledge/product/features/{feature_slug}.md)"
    else:
        feature_line = f"{feature_slug} (brain/knowledge/product/features/{feature_slug}.md not yet written)"

    grouped: dict[str, list[HypothesisDraft]] = {key: [] for key in RISK_HEADINGS}
    for h in hypotheses:
        grouped[h.risk].append(h)

    meta = "\n".join(
        [
            f"- Feature: {feature_line}",
            f"- Created: {today}",
            f"- Last updated: {today}",
        ]
    )
    parts = [f"# Hypotheses — {title}", _section("Meta", meta)]

    for risk_key, (heading, code) in RISK_HEADINGS.items():
        blocks = [_render_hypothesis_block(code, idx, h) for idx, h in enumerate(grouped[risk_key], start=1)]
        body = "\n\n".join(blocks)
        parts.append(_section(heading, body))

    return "\n\n".join(parts) + "\n"


def _render_ingestion_text(*, title: str, date: str, summary: str, claims: Sequence[tuple[str, str]]) -> str:
    source_line = "no artifact, verbal only"
    for _text, tag in claims:
        row_parse = parse_row(tag)
        if row_parse.ok and row_parse.tags and row_parse.tags[0].kind is TagKind.source:
            t = row_parse.tags[0]
            source_line = f"[{t.link_text}]({t.target_path})"
            break

    synthesis_body = summary.strip()
    claim_lines = _render_pairs(claims)
    if claim_lines:
        synthesis_body = f"{synthesis_body}\n\n{claim_lines}" if synthesis_body else claim_lines

    parts = [
        f"# {title}",
        _section("Source", source_line),
        _section("Date", date),
        _section("Synthesis", synthesis_body),
    ]
    return "\n\n".join(parts) + "\n"


# --------------------------------------------------------------------------- promotability


def _promote_decision_status(rendered: str) -> str:
    """A copy of a rendered ``pending`` decision with ``## Status`` flipped to the
    value a human promotion sets (``decided``) — used only to run the promoted-status
    validate pass below. Never written to disk; the real file always stays pending."""
    pending_block = _section("Status", "pending")
    decided_block = _section("Status", DecisionStatus.decided.value)
    return rendered.replace(pending_block, decided_block, 1)


def _promote_hypothesis_status(rendered: str) -> str:
    """Same idea for a hypotheses file: every rendered ``- **Status:** open`` line
    (there is one per hypothesis in the file) flipped to ``supported``, the value a
    human promotion sets. A file can hold several hypotheses; all of them are checked
    as promoted, since none of ``validate_content``'s checks read the specific status
    value beyond "is this a valid enum member", so which one(s) a human would actually
    promote first makes no difference to the result."""
    open_line = "- **Status:** open"
    supported_line = f"- **Status:** {HypothesisStatus.supported.value}"
    return rendered.replace(open_line, supported_line)


def _wrap_promotion_finding(finding: Finding, *, target: Path, promoted_status: str) -> Finding:
    """Re-label an error found by the promoted-status pass so it reads as what it is:
    not a defect in the pending file on disk (there isn't one — the pending-status
    pass already found this draft clean), but a reason the *promotion* a human is
    meant to do with a one-line status edit would itself be bounced back."""
    return Finding(
        path=target,
        line=finding.line,
        code="not_promotable",
        message=(
            f"this draft would be rejected once promoted to '{promoted_status}' "
            f"({finding.code}: {finding.message}) — fix that before drafting, or the "
            "promotion a human makes will fail instead of this draft"
        ),
        severity="error",
    )


# --------------------------------------------------------------------------- validate-then-write


def _finish(
    target: Path,
    rendered: str,
    brain_root: Path,
    *,
    dry_run: bool,
    promoted_rendered: str | None = None,
    promoted_status: str | None = None,
) -> DraftResult:
    """PLATFORM.md §12.3.3, extended: validate the rendered (pending/open) text
    against the real target path exactly as before, and write only when it is
    error-free — that gate is unchanged and still refuses on its own terms.

    When ``promoted_rendered`` is given (decisions and hypotheses, not ingestion,
    which carries no lifecycle status), a drafted record must also be *promotable*:
    once the pending-status pass finds no error, a second pass validates a copy of
    the same text with its status already flipped to ``promoted_status``. Any
    error-level finding from that second pass refuses the draft too — a draft that
    would fail the moment a human promotes it is refused now instead of at their
    desk later. This is additive only: it can refuse a draft the first pass would
    have accepted, but it never overrides or loosens what the first pass already
    refused.

    Never touches disk under dry_run or on either refusal.
    """
    findings = validate_content(rendered, path=target, brain_root=brain_root)
    if any(f.severity == "error" for f in findings):
        return DraftResult(path=None, written=False, findings=findings, rendered=rendered)

    if promoted_rendered is not None:
        promotion_findings = validate_content(promoted_rendered, path=target, brain_root=brain_root)
        promotion_errors = [f for f in promotion_findings if f.severity == "error"]
        if promotion_errors:
            wrapped = [
                _wrap_promotion_finding(f, target=target, promoted_status=promoted_status or "")
                for f in promotion_errors
            ]
            return DraftResult(path=None, written=False, findings=findings + wrapped, rendered=rendered)

    if dry_run:
        return DraftResult(path=None, written=False, findings=findings, rendered=rendered)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8")
    return DraftResult(path=target, written=True, findings=findings, rendered=rendered)


# --------------------------------------------------------------------------- public API


def draft_decision(
    brain_root: str | Path,
    *,
    slug: str,
    title: str,
    date: str,
    context: str,
    options: Sequence[str],
    decision: str,
    why: str,
    evidence: Sequence[tuple[str, str]],
    not_doing: Sequence[tuple[str, str]] = (),
    reversal: str | None = None,
    ambiguities: str | None = None,
    effect: dict | None = None,
    dry_run: bool = False,
    allow_replace: bool = False,
) -> DraftResult:
    """Draft a decision file. Always writes ``## Status`` as ``pending`` — there is no
    parameter to change that (PLATFORM.md §12.3.4). ``effect``, if given, is rendered as
    a ``## Quantified effect`` block, which the validator then refuses on a non-decided
    decision (see the module docstring) — the file this produces is a draft to be
    promoted by a human, and only a promotion to ``decided`` may carry a figure."""
    brain_root = Path(brain_root)
    collection_dir = brain_root / "decisions"

    err = _validate_date(date) or _validate_slug(slug, field_name="slug")
    if err:
        return _refuse(collection_dir / "<invalid>.md", "bad_filename", err)

    target = collection_dir / f"{date}-{slug}.md"

    err = _confine(target, brain_root=brain_root, collection_dir=collection_dir)
    if err:
        return _refuse(target, "path_confinement", err)

    err = _check_tag_pairs(evidence, label="evidence") or _check_tag_pairs(not_doing, label="not_doing")
    if err:
        return _refuse(target, "invalid_provenance_tag", err)

    err = _check_decision_overwrite(target, allow_replace=allow_replace)
    if err:
        code = "record_immutable" if "never overwritten" in err else "replace_not_allowed"
        return _refuse(target, code, err)

    rendered = _render_decision_text(
        title=title,
        date=date,
        context=context,
        options=options,
        decision=decision,
        why=why,
        evidence=evidence,
        not_doing=not_doing,
        reversal=reversal,
        ambiguities=ambiguities,
        effect=effect,
    )
    return _finish(
        target,
        rendered,
        brain_root,
        dry_run=dry_run,
        promoted_rendered=_promote_decision_status(rendered),
        promoted_status=DecisionStatus.decided.value,
    )


def draft_hypotheses(
    brain_root: str | Path,
    *,
    feature_slug: str,
    title: str,
    hypotheses: Sequence[HypothesisDraft | dict],
    dry_run: bool = False,
    allow_replace: bool = False,
) -> DraftResult:
    """Draft a feature's hypotheses file. Every hypothesis is always rendered
    ``**Status:** open`` (PLATFORM.md §12.3.4); a ``status`` key on an input dict, if
    present, is never read."""
    brain_root = Path(brain_root)
    collection_dir = brain_root / "hypotheses"

    err = _validate_slug(feature_slug, field_name="feature_slug")
    if err:
        return _refuse(collection_dir / "<invalid>.md", "bad_filename", err)

    target = collection_dir / f"{feature_slug}.md"

    err = _confine(target, brain_root=brain_root, collection_dir=collection_dir)
    if err:
        return _refuse(target, "path_confinement", err)

    try:
        coerced = [_coerce_hypothesis(h) for h in hypotheses]
    except (KeyError, TypeError) as exc:
        return _refuse(target, "bad_hypothesis", f"malformed hypothesis item: {exc}")

    for idx, h in enumerate(coerced):
        if h.risk not in RISK_HEADINGS:
            return _refuse(
                target, "bad_risk_area", f"hypotheses[{idx}].risk {h.risk!r} must be one of {tuple(RISK_HEADINGS)}"
            )
        err = _check_tag_pairs(h.evidence_for, label=f"hypotheses[{idx}].evidence_for") or _check_tag_pairs(
            h.evidence_against, label=f"hypotheses[{idx}].evidence_against"
        )
        if err:
            return _refuse(target, "invalid_provenance_tag", err)

    err = _check_hypothesis_overwrite(target, allow_replace=allow_replace)
    if err:
        code = "record_immutable" if "never overwritten" in err else "replace_not_allowed"
        return _refuse(target, code, err)

    rendered = _render_hypotheses_text(title=title, feature_slug=feature_slug, brain_root=brain_root, hypotheses=coerced)
    return _finish(
        target,
        rendered,
        brain_root,
        dry_run=dry_run,
        promoted_rendered=_promote_hypothesis_status(rendered),
        promoted_status=HypothesisStatus.supported.value,
    )


def draft_ingestion(
    brain_root: str | Path,
    *,
    collection: str,
    slug: str,
    date: str,
    title: str,
    summary: str,
    claims: Sequence[tuple[str, str]],
    dry_run: bool = False,
) -> DraftResult:
    """Draft an ingestion record under ``ingestion/<collection>/``. Ingestion files
    carry no lifecycle status (PLATFORM.md §12.4), so there is no ``allow_replace``:
    an existing file at the target path is refused outright (``_check_ingestion_overwrite``)."""
    brain_root = Path(brain_root)

    err = _validate_collection(collection) or _validate_date(date) or _validate_slug(slug, field_name="slug")
    if err:
        return _refuse(brain_root / "ingestion" / "<invalid>.md", "bad_filename", err)

    collection_dir = brain_root / "ingestion" / collection
    target = collection_dir / f"{date}-{slug}.md"

    err = _confine(target, brain_root=brain_root, collection_dir=collection_dir)
    if err:
        return _refuse(target, "path_confinement", err)

    err = _check_tag_pairs(claims, label="claims")
    if err:
        return _refuse(target, "invalid_provenance_tag", err)

    err = _check_ingestion_overwrite(target)
    if err:
        return _refuse(target, "record_immutable", err)

    rendered = _render_ingestion_text(title=title, date=date, summary=summary, claims=claims)
    return _finish(target, rendered, brain_root, dry_run=dry_run)
