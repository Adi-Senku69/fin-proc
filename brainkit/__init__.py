"""brainkit — parse, validate, reindex and (phase B1) write the markdown brain
(PLATFORM.md §5, §9, §10 P1, §12 B1).

``brain/`` markdown files are the source of truth. This package turns them into the
derived index (``provenance``'s SQLAlchemy models) and enforces, at parse/validate
time and again at reindex time, the provenance contract in PLATFORM.md §4.
``brainkit.writer`` is the only sanctioned way to create a brain file (PLATFORM.md §12).
"""

from brainkit.writer import (
    DraftResult,
    HypothesisDraft,
    draft_decision,
    draft_hypotheses,
    draft_ingestion,
)

__all__ = [
    "DraftResult",
    "HypothesisDraft",
    "draft_decision",
    "draft_hypotheses",
    "draft_ingestion",
]
