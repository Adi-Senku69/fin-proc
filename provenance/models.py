"""SQLAlchemy 2.x models for the provenance index tables (PLATFORM.md §6).

Tables: claim, evidence, claim_link. This is the **derived index** over the markdown source
of truth under ``brain/`` (PLATFORM.md §3): never edit these rows to change a fact, edit the
file and re-ingest.

Hard rule enforced here (the judgment-side equivalent of ``plan_value.derivation_id`` in
nvplan/db/models.py): ``evidence.claim_id`` is a **NOT NULL** foreign key, so an unsourced
claim cannot be stored.

Decoupling note - read this before adding a ForeignKey
--------------------------------------------------------
This module defines its **own** ``Base`` / metadata; it does NOT import nvplan's ``Base``.
The two metadata sets stay separate for now so ``provenance`` and ``nvplan`` remain
independently importable and testable packages that don't need each other's engine.

Columns that logically point at an nvplan row - ``Claim.derivation_id`` (-> nvplan
``derivation.id``), ``Claim.ai_record_id`` (-> nvplan ``ai_record.id``) - are plain
``Integer`` columns with **no** ``ForeignKey`` constraint. The same choice is made for
``Evidence.target_claim_id`` and ``Evidence.target_derivation_id``: even though
PLATFORM.md §6 glosses them as "FK-nullable", they are left as plain nullable integers
here, because at ingest time the target they name (another claim not yet ingested, or an
nvplan derivation) may not exist yet, and a real FK would make ingest order-dependent. The
P2 bridge (PLATFORM.md §7) is what resolves and validates these references at ingest time,
not a DB constraint. ``ClaimLink.from_claim_id``/``to_claim_id`` DO get a real ForeignKey:
both ends are always local ``claim`` rows created in the same index.

``Claim.effect_json`` (PLATFORM.md §7.1, P2 bridge) is the indexed form of a decision's
optional ``## Quantified effect`` markdown block (``{"category", "year", "value",
"unit"}``), null for every claim that carries no such block. It is a read-optimized cache
only: the markdown file remains the source of truth, and this column is rebuilt from it on
every re-ingest exactly like every other indexed field.
"""

from __future__ import annotations

import enum
from datetime import date as date_, datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Engine,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from provenance.tags import TagKind


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- enums


class ClaimKind(enum.Enum):
    decision = "decision"
    hypothesis = "hypothesis"
    ingestion = "ingestion"
    knowledge = "knowledge"
    computed = "computed"
    ai_proposal = "ai_proposal"


class EvidenceSection(enum.Enum):
    evidence_for = "evidence_for"
    evidence_against = "evidence_against"
    not_doing = "not_doing"


class LinkRelation(enum.Enum):
    supersedes = "supersedes"
    tests = "tests"
    informs = "informs"
    quantifies = "quantifies"


# --------------------------------------------------------------------------- base


class Base(DeclarativeBase):
    type_annotation_map: dict[Any, Any] = {}


# --------------------------------------------------------------------------- tables


class Claim(Base):
    __tablename__ = "claim"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[ClaimKind] = mapped_column(Enum(ClaimKind), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False)
    path: Mapped[str | None] = mapped_column(String(1024), unique=True, nullable=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Holds the lifecycle value (DecisionStatus/HypothesisStatus/AiStatus) as text: different
    # claim kinds have different lifecycles, so this is not one closed Enum column.
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    date: Mapped[date_ | None] = mapped_column(Date, nullable=True)
    body_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Plain integer, no ForeignKey - see module docstring.
    derivation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ai_record_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    # Indexed form of a decision's optional "## Quantified effect" block - see module
    # docstring. Null for every claim that carries no such block.
    effect_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    evidence: Mapped[list[Evidence]] = relationship(back_populates="claim", foreign_keys="Evidence.claim_id")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Claim {self.kind.value} {self.slug!r}>"


class Evidence(Base):
    __tablename__ = "evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # NOT NULL: an unsourced claim cannot be stored (PLATFORM.md §6).
    claim_id: Mapped[int] = mapped_column(ForeignKey("claim.id"), nullable=False)
    section: Mapped[EvidenceSection] = mapped_column(Enum(EvidenceSection), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    tag_kind: Mapped[TagKind] = mapped_column(Enum(TagKind), nullable=False)
    tag_raw: Mapped[str] = mapped_column(Text, nullable=False)
    target_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # Plain integers, no ForeignKey - see module docstring.
    target_claim_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_derivation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    claim: Mapped[Claim] = relationship(back_populates="evidence", foreign_keys=[claim_id])


class ClaimLink(Base):
    __tablename__ = "claim_link"
    __table_args__ = (
        UniqueConstraint("from_claim_id", "to_claim_id", "relation", name="uq_claim_link_triple"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    from_claim_id: Mapped[int] = mapped_column(ForeignKey("claim.id"), nullable=False)
    to_claim_id: Mapped[int] = mapped_column(ForeignKey("claim.id"), nullable=False)
    relation: Mapped[LinkRelation] = mapped_column(Enum(LinkRelation), nullable=False)


ALL_TABLES = [Claim, Evidence, ClaimLink]


# --------------------------------------------------------------------------- engine / session

DEFAULT_DB_URL = "sqlite:///provenance.db"


def get_engine(url: str | None = None, **kwargs: Any) -> Engine:
    """Create an engine. SQLite engines get ``PRAGMA foreign_keys=ON`` so FK constraints
    (incl. the NOT NULL claim FKs) are actually enforced, mirroring nvplan/db/session.py."""
    url = url or DEFAULT_DB_URL
    engine = create_engine(url, **kwargs)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _enable_fk(dbapi_connection, _record):  # pragma: no cover - trivial
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def init_db(engine: Engine) -> Engine:
    """Create all tables on ``engine``."""
    Base.metadata.create_all(engine)
    return engine
