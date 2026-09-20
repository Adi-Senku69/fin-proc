"""bridge/draft.py — the assistant drafts decisions and hypotheses from a question
(PLATFORM.md §12.6, work package B3).

B1 built the write substrate (``brainkit.writer.draft_decision``/``draft_hypotheses``:
render, validate-before-write, path confinement, status never the caller's to choose) and
B2 gave that substrate its first real caller for ingestion (``bridge.ingest.
write_env_scan_ingestion``). Nothing yet let an agent originate a *decision* or a
*hypothesis* record - the one path PLATFORM.md §12.1 exists to make safe: "the model
proposes, a named human confirms, and nothing unconfirmed reaches a number." This module
is that second caller, following ``bridge/ingest.py``'s own shape exactly: render ->
validate -> write -> reindex -> hand back a claim id.

Why this lives in bridge/, not nvplan/ai
------------------------------------------
Same reason as ``bridge/ingest.py`` (see its own docstring): this is the one seam that
needs both ``brainkit`` (the writer, the indexer) and a live ``Session``/``Claim`` row to
read back an id, and PLATFORM.md §7.1 restricts importing both ``nvplan`` and ``brainkit``
to ``bridge/``. This module itself imports neither ``nvplan`` nor anything from
``nvplan.ai`` - like ``bridge/ingest.py``, it takes a plain SQLAlchemy ``Session`` and a
path, so ``nvplan.ai.tools`` is what closes the loop by importing this module (never
``brainkit`` directly), exactly as ``nvplan.ai.agents`` already does for ``bridge.ingest``.

Deliberately narrower than the B1 signature it wraps
--------------------------------------------------------
``draft_decision_and_index`` has no ``effect`` parameter at all, even though
``brainkit.writer.draft_decision`` accepts one. A quantified effect is only ever valid on
a ``decided`` decision (PLATFORM.md §7.1), and a drafted decision is never anything but
``pending`` - the writer already refuses any effect on a pending draft
(``effect_on_undecided``), unconditionally, every time. Exposing the parameter here anyway
would just be a channel that always ends in a refusal, one more argument for a caller (or a
hostile prompt) to poke at for no reachable outcome. Closing it off structurally, at the
one seam an AI-facing tool is built from, is cheaper and more legible than trusting the
refusal to fire correctly on every future caller. There is no ``status`` parameter either,
for the same reason the writer itself has none - inherited unchanged, never re-added here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from brainkit.indexer import reindex_tree
from brainkit.writer import DraftResult, draft_decision as _draft_decision, draft_hypotheses as _draft_hypotheses

from provenance.models import Claim

__all__ = ["DraftOutcome", "draft_decision_and_index", "draft_hypotheses_and_index"]


@dataclass(frozen=True)
class DraftOutcome:
    """What one draft-and-index call actually did - the same shape as
    ``bridge.ingest.IngestOutcome`` for the same reason: ``claim_id`` is None both when the
    file was never written (the draft was refused - see ``result.findings``) and when it WAS
    written but this session could not index it (no provenance tables). ``result.written``
    tells the two cases apart."""

    result: DraftResult
    claim_id: int | None


def _index_after_write(session: Session, brain_root: Path, result: DraftResult) -> int | None:
    """``bridge.ingest.write_env_scan_ingestion``'s own indexing step, factored out because
    both draft kinds need exactly it: reindex in a PRIVATE session opened against the
    caller's own bind (never the caller's ``session`` object, so a missing claim/evidence
    schema is contained here and never rolls back or expires anything the caller's session
    already holds - see that module's docstring for the incident this avoids), then look the
    fresh claim up by the path we just wrote."""
    if not result.written or result.path is None:
        return None
    path_str = str(result.path.resolve().relative_to(brain_root.resolve()).as_posix())
    try:
        with Session(session.get_bind()) as index_session:
            reindex_tree(index_session, brain_root, strict=True)
            claim = index_session.execute(select(Claim).where(Claim.path == path_str)).scalar_one_or_none()
            return claim.id if claim is not None else None
    except Exception:  # noqa: BLE001 - e.g. a finance-only database with no claim/evidence tables
        return None


def draft_decision_and_index(
    session: Session,
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
) -> DraftOutcome:
    """Draft a decision (``brainkit.writer.draft_decision`` - always ``pending``, see the
    module docstring for why there is no ``effect``/``status`` parameter here), then index it
    so the caller gets back a real claim id or a reason it was refused. Never overwrites an
    existing file (``allow_replace`` stays at its default ``False``): a hostile or careless
    re-ask that reuses a slug/date already on disk is refused rather than silently replacing
    a draft someone may already be reviewing."""
    brain_root = Path(brain_root)
    result = _draft_decision(
        brain_root,
        slug=slug,
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
        effect=None,
    )
    return DraftOutcome(result=result, claim_id=_index_after_write(session, brain_root, result))


def draft_hypotheses_and_index(
    session: Session,
    brain_root: str | Path,
    *,
    feature_slug: str,
    title: str,
    hypotheses: Sequence[Any],
) -> DraftOutcome:
    """Draft a feature's hypotheses file (``brainkit.writer.draft_hypotheses`` - every
    hypothesis always renders ``open``), then index it. ``hypotheses`` is passed straight
    through to the B1 writer as plain dicts (never a ``brainkit.writer.HypothesisDraft`` -
    this module's caller, ``nvplan.ai.tools``, must not import ``brainkit`` itself, so it
    cannot construct one); the writer's own ``_coerce_hypothesis`` builds the real dataclass
    from each dict and - the same discipline this whole phase turns on - never reads a
    ``status`` key even if one is present in it."""
    brain_root = Path(brain_root)
    result = _draft_hypotheses(brain_root, feature_slug=feature_slug, title=title, hypotheses=hypotheses)
    return DraftOutcome(result=result, claim_id=_index_after_write(session, brain_root, result))
