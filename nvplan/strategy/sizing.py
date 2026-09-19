"""Market sizing: TAM / SAM / SOM arithmetic over stated assumptions.

PLATFORM.md §8 draws the line: would two competent people, given the same inputs, produce the
same answer? Population times penetration times price, cut down by a serviceable share and then
an obtainable share, is arithmetic - so it is code, not a skill, and every figure it produces
registers a :class:`~nvplan.core.ledger.Derivation` in the caller's ledger, exactly as
:mod:`nvplan.core.regression` registers ``param:*`` and ``plan:default:*``. A sizing figure opens
like any other number in the plan: formula text, inputs, and parents.

Every :class:`SizingInput` carries a ``source_tag``: a provenance tag from the closed enum in
PLATFORM.md §4.1, validated with :func:`provenance.parse_tags` before any arithmetic runs. An
input whose tag is absent or malformed is rejected - an unsourced assumption is exactly what this
platform refuses everywhere else. Shares (penetration, serviceable, obtainable) must be in
``(0, 1]``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from provenance import parse_tags

from nvplan.core.ledger import DerivationLedger

__all__ = [
    "SizingInput",
    "SizingResult",
    "market_size",
    "tam_key",
    "sam_key",
    "som_key",
]


# --------------------------------------------------------------------------- keys


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    if not slug:
        raise ValueError(f"label {text!r} has no usable characters for a derivation key")
    return slug


def tam_key(label: str) -> str:
    return f"sizing:{_slugify(label)}:tam"


def sam_key(label: str) -> str:
    return f"sizing:{_slugify(label)}:sam"


def som_key(label: str) -> str:
    return f"sizing:{_slugify(label)}:som"


# --------------------------------------------------------------------------- validation


def _validate_source_tag(source_tag: str, *, context: str) -> None:
    """Reject a ``source_tag`` that is absent or malformed (PLATFORM.md §4.1, §4.2).

    A well-formed tag is exactly one valid tag from the closed enum. Zero matches means the
    tag is missing or unrecognizable; more than one match means the string is not a single
    clean tag either.
    """
    if not source_tag or not source_tag.strip():
        raise ValueError(f"{context}: source_tag is missing")
    tags = parse_tags(source_tag)
    if len(tags) != 1:
        raise ValueError(
            f"{context}: source_tag {source_tag!r} must carry exactly one valid provenance tag "
            f"from the closed enum in PLATFORM.md §4.1 (found {len(tags)})"
        )


def _validate_share(value: float, name: str) -> float:
    value = float(value)
    if not (0.0 < value <= 1.0):
        raise ValueError(f"{name} must be in (0, 1], got {value!r}")
    return value


def _validate_positive(value: float, name: str) -> float:
    value = float(value)
    if not value > 0.0:
        raise ValueError(f"{name} must be positive, got {value!r}")
    return value


# --------------------------------------------------------------------------- dataclasses


@dataclass(frozen=True)
class SizingInput:
    """The stated assumptions behind a total-addressable-market figure.

    ``penetration`` is the share of ``population`` that is a plausible buyer at all (a rate, not
    a headcount), so it is validated as a share like ``serviceable_share``/``obtainable_share``
    in :func:`market_size`. ``source_tag`` must be a single valid PLATFORM.md §4.1 provenance
    tag - the whole figure is refused without one.
    """

    population: float
    penetration: float
    price: float
    label: str
    source_tag: str

    def __post_init__(self) -> None:
        _validate_positive(self.population, "population")
        _validate_share(self.penetration, "penetration")
        _validate_positive(self.price, "price")
        if not self.label or not self.label.strip():
            raise ValueError("label must be non-empty")
        _validate_source_tag(self.source_tag, context=f"SizingInput({self.label!r})")


@dataclass(frozen=True)
class SizingResult:
    tam: float
    sam: float
    som: float
    unit: str
    derivation_keys: dict[str, str]


# --------------------------------------------------------------------------- the arithmetic


def market_size(
    total: SizingInput,
    serviceable_share: float,
    obtainable_share: float,
    *,
    unit: str = "kEUR",
    ledger: DerivationLedger,
) -> SizingResult:
    """TAM = population * penetration * price; SAM = TAM * serviceable_share;
    SOM = SAM * obtainable_share.

    Registers all three figures in ``ledger`` with formula text, inputs, and parents - TAM has
    no parent derivation (it is rooted in ``total``'s stated, tagged assumptions), SAM's parent
    is the TAM key, SOM's parent is the SAM key. Returns the three figures plus the keys a
    caller cites (e.g. as ``(computed, <derivation-key>)`` in a brain claim).
    """
    serviceable_share = _validate_share(serviceable_share, "serviceable_share")
    obtainable_share = _validate_share(obtainable_share, "obtainable_share")

    tam = total.population * total.penetration * total.price
    sam = tam * serviceable_share
    som = sam * obtainable_share

    k_tam, k_sam, k_som = tam_key(total.label), sam_key(total.label), som_key(total.label)

    ledger.add(
        k_tam,
        "TAM = population * penetration * price",
        inputs={
            "population": total.population,
            "penetration": total.penetration,
            "price": total.price,
            "label": total.label,
            "source_tag": total.source_tag,
            "unit": unit,
        },
        parents=(),
    )
    ledger.add(
        k_sam,
        "SAM = TAM * serviceable_share",
        inputs={"tam": tam, "serviceable_share": serviceable_share, "unit": unit},
        parents=(k_tam,),
    )
    ledger.add(
        k_som,
        "SOM = SAM * obtainable_share",
        inputs={"sam": sam, "obtainable_share": obtainable_share, "unit": unit},
        parents=(k_sam,),
    )

    return SizingResult(
        tam=tam,
        sam=sam,
        som=som,
        unit=unit,
        derivation_keys={"tam": k_tam, "sam": k_sam, "som": k_som},
    )
