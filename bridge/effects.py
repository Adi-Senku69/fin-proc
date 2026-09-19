"""bridge/effects.py — decision drives money (PLATFORM.md §7, §7.1).

A ``decided`` decision carrying a quantified effect for ``REV`` and a plan year becomes
the revenue override the planning run already accepts. This module reads the indexed
effects off the claim index (``decided_effects``) and turns the ``REV`` ones into the
``year -> Override`` mapping ``nvplan.services.planning.run_plan`` takes as
``revenue_override`` (``revenue_override``).

Dependency note
----------------
``Override`` and ``DECISION_OVERRIDE_FORMULA`` are added to ``nvplan.services.planning`` by
a parallel change against the same PLATFORM.md §7.1 contract:

    @dataclass(frozen=True)
    class Override:
        value: float
        ai_record_id: int | None = None
        claim_id: int | None = None
        formula_text: str = AI_OVERRIDE_FORMULA
        label: str = ""
    DECISION_OVERRIDE_FORMULA = "confirmed decision"

If that change has not landed yet, the import below fails and is caught; ``Override``
stays ``None`` and :func:`revenue_override` raises a clear ``ImportError`` rather than
inventing a local stand-in for a contract owned elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from provenance.models import Claim, ClaimKind

try:
    from nvplan.services.planning import DECISION_OVERRIDE_FORMULA, Override
except ImportError:  # pragma: no cover - exercised only before the parallel change lands
    Override = None  # type: ignore[assignment]
    DECISION_OVERRIDE_FORMULA = "confirmed decision"

__all__ = ["QuantifiedEffect", "decided_effects", "revenue_override", "WIRED_CATEGORY"]

#: PLATFORM.md §7.1: only REV currently drives the plan.
WIRED_CATEGORY = "REV"


@dataclass(frozen=True)
class QuantifiedEffect:
    claim_id: int
    decision_slug: str
    decision_title: str
    category_code: str
    year: int
    value: float
    unit: str
    status: str
    decided_on: date | None


def decided_effects(session: Session) -> list[QuantifiedEffect]:
    """Every decision ``Claim`` with ``status == "decided"`` and a non-null
    ``effect_json``, newest first (by ``decided_on``, then ``id`` as a stable tiebreaker
    for same-day decisions)."""
    rows = (
        session.execute(
            select(Claim)
            .where(Claim.kind == ClaimKind.decision, Claim.status == "decided", Claim.effect_json.is_not(None))
            .order_by(Claim.date.desc(), Claim.id.desc())
        )
        .scalars()
        .all()
    )
    out: list[QuantifiedEffect] = []
    for c in rows:
        ej = c.effect_json or {}
        if ej.get("category") is None or ej.get("year") is None or ej.get("value") is None:
            continue  # defensive: effect_json is only ever written well-formed, but never trust blindly
        out.append(
            QuantifiedEffect(
                claim_id=c.id,
                decision_slug=c.slug,
                decision_title=c.title or c.slug,
                category_code=str(ej["category"]).strip().upper(),
                year=int(ej["year"]),
                value=float(ej["value"]),
                unit=str(ej.get("unit") or ""),
                status=c.status or "",
                decided_on=c.date,
            )
        )
    return out


def revenue_override(effects: Sequence[QuantifiedEffect]) -> dict[int, "Override"]:
    """``REV``-category effects only -> ``{year: Override(...)}``.

    When two decided decisions target the same year, the newer ``decided_on`` wins and the
    older is skipped from the returned mapping. The shadowed (losing) claim ids are exposed
    as ``revenue_override.last_shadowed`` — a tuple of claim ids, set on this function object
    as a documented side attribute each call, since the function's return type is fixed by
    PLATFORM.md §7.1 to a single ``dict[int, Override]`` with no room for a second value.
    """
    if Override is None:
        raise ImportError(
            "nvplan.services.planning.Override is not available yet (PLATFORM.md §7.1 "
            "addition, owned by a parallel change to nvplan/services/planning.py); "
            "bridge.effects.revenue_override() cannot build Override values without it."
        )

    winners: dict[int, QuantifiedEffect] = {}
    shadowed: list[int] = []
    for eff in effects:
        if eff.category_code != WIRED_CATEGORY:
            continue
        current = winners.get(eff.year)
        if current is None:
            winners[eff.year] = eff
            continue
        cur_date, new_date = current.decided_on, eff.decided_on
        if new_date is not None and (cur_date is None or new_date > cur_date):
            winners[eff.year] = eff
            shadowed.append(current.claim_id)
        else:
            shadowed.append(eff.claim_id)

    revenue_override.last_shadowed = tuple(shadowed)
    return {
        year: Override(
            value=eff.value,
            claim_id=eff.claim_id,
            formula_text=DECISION_OVERRIDE_FORMULA,
            label=eff.decision_slug,
        )
        for year, eff in winners.items()
    }


revenue_override.last_shadowed = ()  # type: ignore[attr-defined]
