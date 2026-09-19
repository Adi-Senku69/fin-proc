"""bridge/lookup.py — money informs decisions (PLATFORM.md §7, §7.1).

A decision's ``(computed, <derivation-key>)`` evidence tag (PLATFORM.md §4.1) names a
ledger key such as ``param:PERS`` or ``plan:base:REV:2027``, not a database id. Resolving
that key to a real ``derivation.id`` is exactly what makes the tag "pin to a specific
calculation over specific inputs, not a number someone retyped" (PLATFORM.md §7). The key
lives nowhere but ``derivation.inputs_json["_key"]`` (see ``nvplan.services.planning``'s
module docstring: "The ``derivation`` table has no key column"), so resolution is a JSON
lookup, not a plain column match.
"""

from __future__ import annotations

from typing import Callable

from sqlalchemy import text
from sqlalchemy.orm import Session

__all__ = ["make_derivation_lookup"]

_QUERY = text("SELECT id FROM derivation WHERE json_extract(inputs_json, '$._key') = :key LIMIT 1")


def make_derivation_lookup(session: Session) -> Callable[[str], "int | None"]:
    """Return a callable ``key -> derivation.id | None``.

    Matches ``json_extract(derivation.inputs_json, '$._key') == key`` (SQLite's JSON1
    functions, available on every modern sqlite3 build). Results are cached inside the
    returned callable, keyed by the ledger key, so repeated lookups for the same key (one
    per evidence row that cites it) hit the database once. Never raises: an unknown key, or
    any lookup failure, resolves to ``None`` so a bad citation is reported by the caller
    (``brainkit.indexer``'s ``Evidence.resolved``) rather than crashing the reindex.
    """
    cache: dict[str, int | None] = {}

    def lookup(key: str) -> int | None:
        if key in cache:
            return cache[key]
        try:
            row = session.execute(_QUERY, {"key": key}).first()
        except Exception:
            row = None
        result = int(row[0]) if row is not None else None
        cache[key] = result
        return result

    return lookup
