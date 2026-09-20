"""bridge/ingest.py — the env-scan writes to the brain, not just the database (PLATFORM.md
§12.4, work package B2).

Until this module existed, ``nvplan.ai.tools.record_external_note`` was a *terminal* write: an
``ExternalNote`` row was the whole record, and reindexing brain/ (PLATFORM.md §3 - "the database
is a derived index, rebuildable at any time by reindexing the tree") could never reconstruct it,
because no markdown file ever backed it. This module is where the env-scan touchpoint's flagged
positions land as a real ``ingestion/market/YYYY-MM-DD-<slug>.md`` record first (via
``brainkit.writer.draft_ingestion`` - validated before it touches disk, exactly like every other
brain write) and are indexed into a ``Claim(kind=ClaimKind.ingestion)`` row second;
``nvplan.ai.agents.run_env_scan`` then re-points its ``ExternalNote`` rows at that claim
(``ExternalNote.source_claim_id`` - additive, see ``nvplan/db/models.py``) instead of leaving them
as the terminal artifact. The file is the record; the ``ExternalNote`` row becomes its
finance-side derived projection of it, exactly as PLATFORM.md §3 frames the database's role.

Why this lives in bridge/, not nvplan/ai
------------------------------------------
This is the one piece of the env-scan touchpoint that needs both ``nvplan`` (to read back a
``Claim`` id with a provenance ``Session``) and ``brainkit`` (``draft_ingestion``/``reindex_tree``).
PLATFORM.md §7.1 is explicit: "``bridge/`` is the only package allowed to import both ``nvplan``
and ``brainkit``". ``nvplan/api`` is a documented, narrower exception to that rule (the
demo/composition layer - see ``nvplan/api/queries.py``'s own import comment), not ``nvplan/ai``.
So ``nvplan.ai.agents`` calls into this module's narrow public API instead of importing
``brainkit`` itself, keeping the "nvplan never imports brainkit" rule intact for the AI layer.

Why every claim carries ``(industry-knowledge)``
---------------------------------------------------
A flagged position is the model's own synthesis over the framework and the company's actuals, not
a citation of one specific interview, transcript or document - so it is never rendered as a
``stakeholder-verbal``/``intuition`` tag (those name a real person and a real date this touchpoint
does not have) nor as a fabricated ``source``/``ingestion`` link (there is no second file to point
at). ``(industry-knowledge)`` (PLATFORM.md §4.1) is the one tag in the closed set that actually
describes what this is: a self-contained analytical assertion, not an unsourced claim - it is a
real, valid, closed-enum tag, checked by the same ``provenance.parse_row`` every other evidence row
answers to, not a workaround for the tag requirement.

Graceful degradation
----------------------
Same discipline as ``nvplan.ai.tools._claims_unavailable``: a session with no provenance tables (a
finance-only database - e.g. the plain demo script, which calls ``nvplan.db.session.init_db``
alone) must not break the scan. The indexing attempt runs in its OWN private ``Session`` opened
against the caller's bind, not the caller's own ``session`` object: a failure there (a missing
``claim`` table) is contained to that private session and reports ``claim_id=None`` - it never
rolls back or expires anything the caller's session already holds (an earlier ``rollback()`` on
the shared session used to do exactly that, expiring an already-committed ``AiRecord`` the caller
still needed to read after this function returned - the markdown file, once written, is
unaffected either way, so only the DB-facing half needs this isolation).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from brainkit.indexer import reindex_tree
from brainkit.writer import DraftResult, draft_ingestion

from provenance.models import Claim

__all__ = ["FINDING_TAG", "INGESTION_COLLECTION", "ScanFinding", "IngestOutcome", "write_env_scan_ingestion"]

#: The one §4.1 tag every env-scan finding carries - see the module docstring.
FINDING_TAG = "(industry-knowledge)"

#: PLATFORM.md §5: the env-scan is an environmental scan, filed under ingestion/market/.
INGESTION_COLLECTION = "market"


@dataclass(frozen=True)
class ScanFinding:
    """One flagged position from an env-scan run (the shape of ``nvplan.ai.schemas.
    FlaggedPosition``), reshaped into what ``draft_ingestion`` needs. Deliberately its own small
    dataclass rather than importing the pydantic schema: this module's only coupling to the AI
    layer's own types would otherwise be a needless import cycle risk for no benefit - the caller
    (``nvplan.ai.agents.run_env_scan``) does the one-line reshape."""

    domain: str
    position: str
    materiality: str
    reasoning: str


@dataclass(frozen=True)
class IngestOutcome:
    """What ``write_env_scan_ingestion`` actually did. ``claim_id`` is None both when the file was
    never written (``draft_ingestion`` refused it - see ``result.findings``) and when it WAS
    written but could not be indexed in this particular session (no provenance tables - see the
    module docstring's graceful-degradation note). The two cases are distinguishable via
    ``result.written``."""

    result: DraftResult
    claim_id: int | None


def _finding_claim(finding: ScanFinding) -> tuple[str, str]:
    text = f"{finding.domain} / {finding.position} ({finding.materiality}): {finding.reasoning}".strip()
    return text, FINDING_TAG


def write_env_scan_ingestion(
    session: Session,
    brain_root: str | Path,
    *,
    slug: str,
    date: str,
    title: str,
    summary: str,
    findings: Sequence[ScanFinding],
) -> IngestOutcome:
    """Draft, validate-before-write and write one ``ingestion/market/<date>-<slug>.md`` record
    from an env-scan run's flagged positions (``brainkit.writer.draft_ingestion`` - the B1
    substrate does the path confinement, the tag check and the validate-then-write gate; nothing
    here bypasses it), then reindex ``brain_root`` so the file's ``Claim`` row exists.

    Never raises on a refused draft or a missing provenance schema: the caller reads
    ``IngestOutcome`` to decide whether it has a real claim id to attach to anything.
    """
    brain_root = Path(brain_root)
    claims = [_finding_claim(f) for f in findings]
    result = draft_ingestion(
        brain_root,
        collection=INGESTION_COLLECTION,
        slug=slug,
        date=date,
        title=title,
        summary=summary,
        claims=claims,
    )
    if not result.written or result.path is None:
        return IngestOutcome(result=result, claim_id=None)

    path_str = str(result.path.resolve().relative_to(brain_root.resolve()).as_posix())
    claim_id: int | None = None
    try:
        with Session(session.get_bind()) as index_session:
            reindex_tree(index_session, brain_root, strict=True)
            claim = index_session.execute(select(Claim).where(Claim.path == path_str)).scalar_one_or_none()
            claim_id = claim.id if claim is not None else None
    except Exception:  # noqa: BLE001 - e.g. a finance-only database with no claim/evidence tables
        claim_id = None

    return IngestOutcome(result=result, claim_id=claim_id)
