"""The provenance tag enum and parser (PLATFORM.md §4.1, §4.2).

Every bullet under an evidence heading in a brain/ markdown file must carry exactly one tag
from the closed set below. This module recognizes the tags, rejects anything that merely
looks like one (a bare parenthetical with no keyword, a markdown link that is not an
``ingestion``/``source`` link), and reports precisely why a row failed.

Scanning ignores backtick inline code spans and fenced code blocks: a tag-shaped string
written as an example inside code is never treated as a real tag.
"""

from __future__ import annotations

import datetime
import enum
import os
import re
from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------------- tag enum


class TagKind(enum.Enum):
    ingestion = "ingestion"  # [ingestion/<path>](../ingestion/<path>)
    source = "source"  # [source/<path>](../source/<path>)
    stakeholder_verbal = "stakeholder_verbal"  # (stakeholder-verbal, <name>, <YYYY-MM-DD>)
    intuition = "intuition"  # (intuition, <role>, <YYYY-MM-DD>)
    industry_knowledge = "industry_knowledge"  # (industry-knowledge)
    chat = "chat"  # (chat, no artifact)
    computed = "computed"  # (computed, <derivation-key>)


@dataclass(frozen=True)
class Tag:
    kind: TagKind
    raw: str  # the tag exactly as written
    target_path: str | None = None  # for ingestion/source: the path inside the link parens
    link_text: str | None = None  # for ingestion/source: the link label
    who: str | None = None  # stakeholder name or intuition role
    date: datetime.date | None = None  # for stakeholder_verbal / intuition
    derivation_key: str | None = None  # for computed


@dataclass(frozen=True)
class RowParse:
    text: str  # the claim with tags stripped and whitespace collapsed
    tags: tuple[Tag, ...]
    ok: bool
    errors: tuple[str, ...]


# --------------------------------------------------------------------------- code-span handling

_FENCED_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


def strip_code_spans(text: str) -> str:
    """Blank out fenced code blocks and inline code spans (replaced with a single space
    each) so that tag scanning never matches a tag-shaped string written as example code.
    """
    text = _FENCED_RE.sub(" ", text)
    text = _INLINE_CODE_RE.sub(" ", text)
    return text


def _blank_span(text: str, start: int, end: int) -> str:
    """Return ``text`` with ``text[start:end]`` replaced by spaces, same length."""
    return text[:start] + (" " * (end - start)) + text[end:]


# --------------------------------------------------------------------------- tag patterns

_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")

_TAG_PAREN_RE = re.compile(
    r"""
    \(\s*(?P<sv_kw>stakeholder-verbal)\s*,\s*(?P<sv_who>[^,()]+?)\s*,\s*(?P<sv_date>[^,()\s][^,()]*?)\s*\)
    |
    \(\s*(?P<in_kw>intuition)\s*,\s*(?P<in_who>[^,()]+?)\s*,\s*(?P<in_date>[^,()\s][^,()]*?)\s*\)
    |
    \(\s*(?P<ik_kw>industry-knowledge)\s*\)
    |
    \(\s*(?P<chat_kw>chat)\s*,\s*(?P<chat_arg>no\s+artifact)\s*\)
    |
    \(\s*(?P<comp_kw>computed)\s*,\s*(?P<comp_key>[^()]+?)\s*\)
    """,
    re.IGNORECASE | re.VERBOSE,
)

_PLACEHOLDER_RE = re.compile(r"^<[^<>]+>$")


def _parse_date(text: str) -> tuple[datetime.date | None, str | None]:
    try:
        return datetime.date.fromisoformat(text), None
    except ValueError:
        return None, f"not a valid calendar date: {text!r}"


def _candidate_from_paren_match(m: re.Match[str]) -> dict:
    gd = m.groupdict()
    raw = m.group(0)
    if gd.get("sv_kw") is not None:
        who = gd["sv_who"].strip()
        date_val, err = _parse_date(gd["sv_date"].strip())
        if not who:
            err = err or "stakeholder-verbal tag is missing a name"
        return {"kind": TagKind.stakeholder_verbal, "raw": raw, "who": who, "date": date_val, "error": err}
    if gd.get("in_kw") is not None:
        who = gd["in_who"].strip()
        date_val, err = _parse_date(gd["in_date"].strip())
        if not who:
            err = err or "intuition tag is missing a role"
        return {"kind": TagKind.intuition, "raw": raw, "who": who, "date": date_val, "error": err}
    if gd.get("ik_kw") is not None:
        return {"kind": TagKind.industry_knowledge, "raw": raw, "error": None}
    if gd.get("chat_kw") is not None:
        return {"kind": TagKind.chat, "raw": raw, "error": None}
    if gd.get("comp_kw") is not None:
        key = gd["comp_key"].strip()
        err = None if key else "computed tag is missing a derivation key"
        return {"kind": TagKind.computed, "raw": raw, "derivation_key": key, "error": err}
    raise AssertionError("unreachable: no named group matched")  # pragma: no cover


def _find_candidates(row_text: str) -> list[dict]:
    """Structural tag matches in ``row_text``, in left-to-right order.

    Each candidate is a dict with at least ``kind``, ``raw``, ``span`` and ``error`` (None
    when the candidate is a fully valid tag). Code spans are ignored; a markdown link that
    is not a recognized ingestion/source link is not a candidate at all (it is not a tag).
    """
    code_free = strip_code_spans(row_text)
    candidates: list[dict] = []
    link_spans: list[tuple[int, int]] = []

    for m in _LINK_RE.finditer(code_free):
        label, href = m.group(1), m.group(2)
        if label.startswith("ingestion/") and href.startswith("../ingestion/"):
            candidates.append(
                {
                    "kind": TagKind.ingestion,
                    "raw": m.group(0),
                    "span": m.span(),
                    "target_path": href,
                    "link_text": label,
                    "error": None,
                }
            )
            link_spans.append(m.span())
        elif label.startswith("source/") and href.startswith("../source/"):
            candidates.append(
                {
                    "kind": TagKind.source,
                    "raw": m.group(0),
                    "span": m.span(),
                    "target_path": href,
                    "link_text": label,
                    "error": None,
                }
            )
            link_spans.append(m.span())
        # else: a markdown link that doesn't match the ingestion/source shape is not a tag.

    paren_scan_text = code_free
    for start, end in link_spans:
        paren_scan_text = _blank_span(paren_scan_text, start, end)

    for m in _TAG_PAREN_RE.finditer(paren_scan_text):
        cand = _candidate_from_paren_match(m)
        cand["span"] = m.span()
        candidates.append(cand)

    candidates.sort(key=lambda c: c["span"][0])
    return candidates


def _strip_spans_and_collapse(text: str, spans: list[tuple[int, int]]) -> str:
    spans = sorted(spans, key=lambda s: s[0])
    parts = []
    last = 0
    for start, end in spans:
        parts.append(text[last:start])
        last = end
    parts.append(text[last:])
    return re.sub(r"\s+", " ", "".join(parts)).strip()


# --------------------------------------------------------------------------- public API


def parse_tags(row_text: str) -> list[Tag]:
    """Every *valid* tag found in one bullet's text (order preserved). A structural match
    that fails validation (e.g. a bad date) is not included here; see ``parse_row`` for the
    row-level error report."""
    tags: list[Tag] = []
    for c in _find_candidates(row_text):
        if c.get("error"):
            continue
        tags.append(
            Tag(
                kind=c["kind"],
                raw=c["raw"],
                target_path=c.get("target_path"),
                link_text=c.get("link_text"),
                who=c.get("who"),
                date=c.get("date"),
                derivation_key=c.get("derivation_key"),
            )
        )
    return tags


def parse_row(row_text: str) -> RowParse:
    """The whole verdict for one evidence bullet: exactly one valid tag is required."""
    candidates = _find_candidates(row_text)
    code_free = strip_code_spans(row_text)
    stripped_text = _strip_spans_and_collapse(code_free, [c["span"] for c in candidates])

    errors: list[str] = []
    if not candidates:
        if _PLACEHOLDER_RE.match(row_text.strip()):
            errors.append(f"empty placeholder: row is a template placeholder, not a claim: {row_text.strip()!r}")
        else:
            errors.append("orphan: row carries no provenance tag from the closed §4.1 set")
    elif len(candidates) > 1:
        errors.append(
            f"multiple tags on one row ({len(candidates)} found); exactly one tag is required per evidence bullet"
        )
    elif candidates[0]["error"]:
        errors.append(f"invalid tag: {candidates[0]['error']} (raw: {candidates[0]['raw']!r})")

    tags = tuple(parse_tags(row_text))
    return RowParse(text=stripped_text, tags=tags, ok=not errors, errors=tuple(errors))


def resolve_path(tag: Tag, *, containing_file: Path, brain_root: Path) -> Path | None:
    """The absolute path a path-typed tag (``ingestion``/``source``) points at, resolved
    relative to the containing file's directory. Returns None for non-path tags.

    Purely lexical: this never touches the filesystem (no ``Path.resolve()``, no stat, no
    symlink resolution). Existence checking is the caller's job.
    """
    if tag.target_path is None:
        return None
    containing_file = Path(containing_file)
    brain_root = Path(brain_root)
    abs_containing = containing_file if containing_file.is_absolute() else (brain_root / containing_file)
    joined = abs_containing.parent / tag.target_path
    # Purely lexical normalization (string manipulation only) - never touches the filesystem,
    # unlike Path.resolve()/os.path.realpath which may stat() to resolve symlinks.
    return Path(os.path.normpath(str(joined)))
