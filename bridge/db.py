"""bridge/db.py — one SQLite database, both metadatas (PLATFORM.md §7.1).

The finance module (``nvplan.db.models.Base``) and the provenance index
(``provenance.models.Base``) each define their own SQLAlchemy metadata (by design — see
``provenance/models.py``'s module docstring: the two packages stay independently
importable and testable). The bridge is where that changes: both directions of PLATFORM.md
§7 need one ``Session`` that can read a ``plan_value`` and a ``Claim`` in the same
transaction, which means both sets of tables have to live in the same database file.
"""

from __future__ import annotations

from sqlalchemy import Engine

from nvplan.db.models import Base as NvplanBase
from provenance.models import Base as ProvenanceBase

__all__ = ["init_platform_db"]


def init_platform_db(engine: Engine) -> Engine:
    """Create every ``nvplan`` table and every ``provenance`` table on ``engine``, in one
    database. Idempotent: ``create_all`` only creates tables that don't already exist, so
    calling this again on the same engine is a no-op."""
    NvplanBase.metadata.create_all(engine)
    ProvenanceBase.metadata.create_all(engine)
    return engine
