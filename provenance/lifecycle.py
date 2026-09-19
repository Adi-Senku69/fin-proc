"""Status lifecycle enums for decisions and hypotheses (PLATFORM.md §4.3).

    decision:   pending -> decided -> superseded
    hypothesis: open -> supported | refuted -> superseded

A decided decision is never edited in place; it can only move to ``superseded`` (never back
to ``pending``). ``ALLOWED_TRANSITIONS`` and ``can_transition`` enforce that.
"""

from __future__ import annotations

import enum


class DecisionStatus(enum.Enum):
    pending = "pending"
    decided = "decided"
    superseded = "superseded"


class HypothesisStatus(enum.Enum):
    open = "open"
    supported = "supported"
    refuted = "refuted"
    superseded = "superseded"


# "proposed" is accepted as an input synonym for "pending" (PLATFORM.md §4.3 lists the
# existing ai-proposal lifecycle as proposed -> confirmed/rejected; a decision file that
# borrows that vocabulary should still parse).
_DECISION_SYNONYMS: dict[str, DecisionStatus] = {
    "pending": DecisionStatus.pending,
    "proposed": DecisionStatus.pending,
    "decided": DecisionStatus.decided,
    "superseded": DecisionStatus.superseded,
}

_HYPOTHESIS_SYNONYMS: dict[str, HypothesisStatus] = {
    "open": HypothesisStatus.open,
    "supported": HypothesisStatus.supported,
    "refuted": HypothesisStatus.refuted,
    "superseded": HypothesisStatus.superseded,
}


def parse_decision_status(text: str) -> DecisionStatus:
    key = text.strip().lower()
    if key not in _DECISION_SYNONYMS:
        raise ValueError(
            f"{text!r} is not a valid decision status; allowed values: "
            "pending, decided, superseded (also accepts 'proposed' as a synonym for 'pending')"
        )
    return _DECISION_SYNONYMS[key]


def parse_hypothesis_status(text: str) -> HypothesisStatus:
    key = text.strip().lower()
    if key not in _HYPOTHESIS_SYNONYMS:
        raise ValueError(
            f"{text!r} is not a valid hypothesis status; allowed values: "
            "open, supported, refuted, superseded"
        )
    return _HYPOTHESIS_SYNONYMS[key]


ALLOWED_TRANSITIONS: dict[type, dict[enum.Enum, frozenset[enum.Enum]]] = {
    DecisionStatus: {
        DecisionStatus.pending: frozenset({DecisionStatus.decided}),
        DecisionStatus.decided: frozenset({DecisionStatus.superseded}),
        DecisionStatus.superseded: frozenset(),
    },
    HypothesisStatus: {
        HypothesisStatus.open: frozenset({HypothesisStatus.supported, HypothesisStatus.refuted}),
        HypothesisStatus.supported: frozenset({HypothesisStatus.superseded}),
        HypothesisStatus.refuted: frozenset({HypothesisStatus.superseded}),
        HypothesisStatus.superseded: frozenset(),
    },
}


def can_transition(from_status: enum.Enum, to_status: enum.Enum) -> bool:
    """True iff moving from ``from_status`` to ``to_status`` is allowed. Both arguments must
    be the same lifecycle enum (DecisionStatus or HypothesisStatus); a decided decision may
    only go to superseded, never back to pending."""
    if type(from_status) is not type(to_status):
        return False
    allowed = ALLOWED_TRANSITIONS.get(type(from_status))
    if allowed is None:
        return False
    return to_status in allowed.get(from_status, frozenset())
