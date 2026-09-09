"""Deterministic planning core: pure pandas/numpy functions, no DB access.

Data contract shared by every module here:

* long frame: ``category_code:str, year:int, value:float``
* wide frame: index ``year`` (int), one column per category code (see :func:`to_wide`)

Every core function returns numbers *and* registers a :class:`Derivation` per
number into the :class:`DerivationLedger` it is handed.
"""

from nvplan.core.ledger import (
    CALC_VERSION,
    Derivation,
    DerivationLedger,
    calc_version,
    jsonable,
    to_long,
    to_wide,
)

__all__ = [
    "CALC_VERSION",
    "Derivation",
    "DerivationLedger",
    "calc_version",
    "jsonable",
    "to_long",
    "to_wide",
]
