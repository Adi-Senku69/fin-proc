"""provenance - the shared provenance core (PLATFORM.md §4, §6).

Tag enum + parser, lifecycle status enums, and the index ORM models (Claim/Evidence/
ClaimLink). ``brainkit/`` and ``brain/`` (PLATFORM.md P1) build on this package's public API.
"""

from provenance.lifecycle import (
    ALLOWED_TRANSITIONS,
    DecisionStatus,
    HypothesisStatus,
    can_transition,
    parse_decision_status,
    parse_hypothesis_status,
)
from provenance.models import (
    Base,
    Claim,
    ClaimKind,
    ClaimLink,
    Evidence,
    EvidenceSection,
    LinkRelation,
    get_engine,
    init_db,
)
from provenance.tags import (
    RowParse,
    Tag,
    TagKind,
    parse_row,
    parse_tags,
    resolve_path,
    strip_code_spans,
)

__all__ = [
    "TagKind",
    "Tag",
    "RowParse",
    "parse_tags",
    "parse_row",
    "strip_code_spans",
    "resolve_path",
    "DecisionStatus",
    "HypothesisStatus",
    "parse_decision_status",
    "parse_hypothesis_status",
    "can_transition",
    "ALLOWED_TRANSITIONS",
    "Base",
    "Claim",
    "Evidence",
    "ClaimLink",
    "ClaimKind",
    "EvidenceSection",
    "LinkRelation",
    "get_engine",
    "init_db",
]
