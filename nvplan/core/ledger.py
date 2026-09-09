"""In-memory derivation ledger (pure, no DB).

Every number the core produces is registered here as a :class:`Derivation`
keyed by a stable string (``param:MAT``, ``plan:base:REV:2027``, ``depr:2028``,
...). The services layer later maps keys -> ``derivation.id`` rows; the core
never touches the database.

Also hosts the shared "long frame" <-> "wide frame" helpers used by every core
module:

* long frame: ``category_code:str, year:int, value:float`` (+ optional extras)
* wide frame: indexed by ``year`` (int, sorted), one float column per code
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

#: Version tag written into ``parameter.calc_version`` by the services layer.
CALC_VERSION = "core-1.0"
calc_version = CALC_VERSION

LONG_COLUMNS = ["category_code", "year", "value"]


# --------------------------------------------------------------------------- frames


def to_wide(long_df: pd.DataFrame) -> pd.DataFrame:
    """Pivot a long frame (category_code, year, value) to a wide frame.

    Result: index = ``year`` (int, ascending), columns = category codes (in
    first-seen order), values = float. Duplicate (code, year) pairs raise.
    """
    missing = set(LONG_COLUMNS) - set(long_df.columns)
    if missing:
        raise ValueError(f"long frame missing columns: {sorted(missing)}")
    df = long_df[LONG_COLUMNS].copy()
    df["category_code"] = df["category_code"].astype(str)
    df["year"] = df["year"].astype(int)
    df["value"] = df["value"].astype(float)
    if df.duplicated(["category_code", "year"]).any():
        dup = df[df.duplicated(["category_code", "year"], keep=False)]
        raise ValueError(f"duplicate (category_code, year) pairs in long frame:\n{dup}")
    order = list(dict.fromkeys(df["category_code"]))
    wide = df.pivot(index="year", columns="category_code", values="value")
    wide = wide.reindex(columns=order).sort_index()
    wide.index = wide.index.astype(int)
    wide.index.name = "year"
    wide.columns.name = None
    return wide


def to_long(wide: pd.DataFrame) -> pd.DataFrame:
    """Inverse of :func:`to_wide` (drops NaN cells)."""
    long = wide.rename_axis("year").reset_index().melt(
        id_vars="year", var_name="category_code", value_name="value"
    )
    long = long.dropna(subset=["value"])
    long["year"] = long["year"].astype(int)
    long["value"] = long["value"].astype(float)
    return long[LONG_COLUMNS].sort_values(["category_code", "year"]).reset_index(drop=True)


# --------------------------------------------------------------------------- json hygiene


def jsonable(obj: Any) -> Any:
    """Recursively convert numpy / pandas scalars & containers to plain JSON types.

    Nothing is rounded; floats stay full precision.
    """
    if isinstance(obj, (str, bool)) or obj is None:
        return obj
    if isinstance(obj, (np.integer, int)):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        return float(obj)
    if isinstance(obj, pd.Series):
        return {str(jsonable(k)): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, pd.DataFrame):
        return [jsonable(r) for r in obj.to_dict(orient="records")]
    if isinstance(obj, Mapping):
        return {str(jsonable(k)): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, np.ndarray)):
        return [jsonable(v) for v in obj]
    raise TypeError(f"not JSON-serialisable: {type(obj).__name__}")


# --------------------------------------------------------------------------- derivation


@dataclass(frozen=True)
class Derivation:
    """In-memory mirror of the ``derivation`` table.

    ``parents`` are ledger *keys* of other derivations (not DB ids).
    """

    key: str
    formula_text: str
    inputs: dict[str, Any] = field(default_factory=dict)
    parameters: dict[str, Any] = field(default_factory=dict)
    parents: tuple[str, ...] = ()

    def as_record(self) -> dict[str, Any]:
        """Plain dict in the shape of the DB row (minus ids / timestamps)."""
        return {
            "key": self.key,
            "formula_text": self.formula_text,
            "inputs_json": self.inputs,
            "parameters_json": self.parameters,
            "parent_keys": list(self.parents),
        }


class DerivationLedger:
    """Ordered key -> :class:`Derivation` store with lineage helpers."""

    def __init__(self) -> None:
        self._items: dict[str, Derivation] = {}

    # -- mutation -----------------------------------------------------------
    def add(
        self,
        key: str,
        formula_text: str,
        inputs: Mapping[str, Any] | None = None,
        parameters: Mapping[str, Any] | None = None,
        parents: Iterable[str] = (),
        *,
        replace: bool = False,
    ) -> Derivation:
        """Register a derivation. Duplicate keys raise unless ``replace=True``."""
        if key in self._items and not replace:
            raise KeyError(f"derivation key already registered: {key!r}")
        d = Derivation(
            key=str(key),
            formula_text=str(formula_text),
            inputs=jsonable(dict(inputs or {})),
            parameters=jsonable(dict(parameters or {})),
            parents=tuple(str(p) for p in parents),
        )
        self._items[key] = d
        return d

    # -- access -------------------------------------------------------------
    def get(self, key: str) -> Derivation:
        return self._items[key]

    def __contains__(self, key: object) -> bool:
        return key in self._items

    def __getitem__(self, key: str) -> Derivation:
        return self._items[key]

    def __iter__(self) -> Iterator[Derivation]:
        return iter(self._items.values())

    def __len__(self) -> int:
        return len(self._items)

    def keys(self) -> list[str]:
        return list(self._items)

    def parents_of(self, key: str) -> tuple[str, ...]:
        return self._items[key].parents

    def missing_parents(self) -> dict[str, list[str]]:
        """key -> parent keys referenced but not registered (empty when consistent)."""
        out: dict[str, list[str]] = {}
        for d in self._items.values():
            gone = [p for p in d.parents if p not in self._items]
            if gone:
                out[d.key] = gone
        return out

    def validate(self) -> None:
        gone = self.missing_parents()
        if gone:
            raise KeyError(f"derivations reference unregistered parents: {gone}")

    # -- lineage ------------------------------------------------------------
    def ancestors(self, key: str) -> list[str]:
        """All transitive parents of ``key`` in topological order (parents first)."""
        order: list[str] = []
        seen: set[str] = set()

        def visit(k: str) -> None:
            for p in self._items[k].parents:
                if p not in seen:
                    seen.add(p)
                    if p not in self._items:
                        raise KeyError(f"{k!r} references unregistered parent {p!r}")
                    visit(p)
                    order.append(p)

        visit(key)
        return order

    def topo_order(self) -> list[str]:
        """Keys with every parent before its children (stable w.r.t. insertion)."""
        order: list[str] = []
        state: dict[str, int] = {}  # 1 = visiting, 2 = done

        def visit(k: str) -> None:
            st = state.get(k, 0)
            if st == 2:
                return
            if st == 1:
                raise ValueError(f"cycle in derivation lineage at {k!r}")
            if k not in self._items:
                raise KeyError(f"unregistered parent referenced: {k!r}")
            state[k] = 1
            for p in self._items[k].parents:
                visit(p)
            state[k] = 2
            order.append(k)

        for k in list(self._items):
            visit(k)
        return order

    def records(self) -> list[dict[str, Any]]:
        """All derivations as plain dicts, in topological order."""
        return [self._items[k].as_record() for k in self.topo_order()]


__all__ = [
    "CALC_VERSION",
    "calc_version",
    "Derivation",
    "DerivationLedger",
    "jsonable",
    "to_long",
    "to_wide",
]
