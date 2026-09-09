"""SQLAlchemy 2.x models for the nine planning tables (PLAN.md section 1).

Tables: category, actual, parameter, scenario, derivation, plan_value,
external_note, ai_record, statement_line.

Hard rules enforced here:

* ``plan_value``, ``statement_line`` and ``parameter`` carry a **NOT NULL**
  foreign key to ``derivation`` - a planned number without a derivation cannot
  exist.
* ``parameter`` rows are immutable/versioned: never update one, insert a new row
  per recompute (``calc_version`` / ``computed_at`` distinguish them).
* ``ai_record.status`` defaults to ``proposed``; nothing enters ``plan_value``
  until a human confirms.

Category note: the PDF has five categories (REV, MAT, EXT, PERS, OTH). The
generator also emits a depreciation series, which is a *component* of OTH,
not a sixth category. It is stored as a category row with
``is_component=True`` (code ``DEPR``) so it can be ingested and subtracted from
OTH before regression. Consumers that want "the five categories" must filter
``is_component == False``.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- enums


class CategoryKind(enum.Enum):
    revenue = "revenue"
    cost = "cost"


class CategoryDriver(enum.Enum):
    revenue = "revenue"
    investment = "investment"


class ScenarioKind(enum.Enum):
    best = "best"
    base = "base"
    worst = "worst"


class PlanPath(enum.Enum):
    valorized = "valorized"
    ai_proposed = "ai_proposed"
    cascaded = "cascaded"


class NoteSource(enum.Enum):
    manual = "manual"
    ai_scan = "ai_scan"


class Touchpoint(enum.Enum):
    env_scan = "env_scan"
    revenue_proposal = "revenue_proposal"
    deviation_explanation = "deviation_explanation"


class AiStatus(enum.Enum):
    proposed = "proposed"
    confirmed = "confirmed"
    rejected = "rejected"


class Statement(enum.Enum):
    pl = "pl"
    bs = "bs"
    cf = "cf"


# --------------------------------------------------------------------------- base


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[int]: JSON}


# --------------------------------------------------------------------------- tables


class Category(Base):
    __tablename__ = "category"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[CategoryKind] = mapped_column(Enum(CategoryKind), nullable=False)
    # Revenue has no driver -> nullable.
    driver: Mapped[CategoryDriver | None] = mapped_column(Enum(CategoryDriver), nullable=True)
    # True for helper series that are part of another category (DEPR within OTH).
    is_component: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    actuals: Mapped[list[Actual]] = relationship(back_populates="category")
    parameters: Mapped[list[Parameter]] = relationship(back_populates="category")
    plan_values: Mapped[list[PlanValue]] = relationship(back_populates="category")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Category {self.code} {self.kind.value}/{self.driver.value if self.driver else '-'}>"


class Actual(Base):
    __tablename__ = "actual"
    __table_args__ = (UniqueConstraint("category_id", "year", name="uq_actual_category_year"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("category.id"), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    source_label: Mapped[str] = mapped_column(String(64), nullable=False)

    category: Mapped[Category] = relationship(back_populates="actuals")


class Derivation(Base):
    """The ledger entry that explains a number: formula, inputs, parameters, parents."""

    __tablename__ = "derivation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    formula_text: Mapped[str] = mapped_column(Text, nullable=False)
    inputs_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    parameters_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    # Derivation ids this derivation depends on (lineage tree edges).
    parent_ids_json: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)

    parameters: Mapped[list[Parameter]] = relationship(back_populates="derivation")
    plan_values: Mapped[list[PlanValue]] = relationship(back_populates="derivation")
    statement_lines: Mapped[list[StatementLine]] = relationship(back_populates="derivation")


class Parameter(Base):
    """Regression output per category. Immutable: insert a new row per recompute."""

    __tablename__ = "parameter"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("category.id"), nullable=False)
    alpha: Mapped[float] = mapped_column(Float, nullable=False)
    beta: Mapped[float] = mapped_column(Float, nullable=False)
    r_squared: Mapped[float] = mapped_column(Float, nullable=False)
    valorization_rate: Mapped[float] = mapped_column(Float, nullable=False)
    window_from: Mapped[int] = mapped_column(Integer, nullable=False)
    window_to: Mapped[int] = mapped_column(Integer, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    calc_version: Mapped[str] = mapped_column(String(32), nullable=False)
    derivation_id: Mapped[int] = mapped_column(ForeignKey("derivation.id"), nullable=False)

    category: Mapped[Category] = relationship(back_populates="parameters")
    derivation: Mapped[Derivation] = relationship(back_populates="parameters")


class Scenario(Base):
    __tablename__ = "scenario"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[ScenarioKind] = mapped_column(Enum(ScenarioKind), nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    plan_values: Mapped[list[PlanValue]] = relationship(back_populates="scenario")
    statement_lines: Mapped[list[StatementLine]] = relationship(back_populates="scenario")


class AiRecord(Base):
    """One AI touchpoint invocation: literal prompt, response, rationale, status."""

    __tablename__ = "ai_record"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    touchpoint: Mapped[Touchpoint] = mapped_column(Enum(Touchpoint), nullable=False)
    prompt_text: Mapped[str] = mapped_column(Text, nullable=False)
    response_text: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    proposed_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    scenario_id: Mapped[int | None] = mapped_column(ForeignKey("scenario.id"), nullable=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("category.id"), nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[AiStatus] = mapped_column(Enum(AiStatus), nullable=False, default=AiStatus.proposed)
    confirmed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    # Per-model-call audit log (nvplan.ai.audit): what was literally sent to and returned by
    # the model on every call of the run, incl. summarization / eviction / clearing markers.
    call_log_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)

    scenario: Mapped[Scenario | None] = relationship()
    category: Mapped[Category | None] = relationship()


class PlanValue(Base):
    __tablename__ = "plan_value"
    __table_args__ = (
        UniqueConstraint("scenario_id", "category_id", "year", name="uq_plan_value_scenario_category_year"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scenario_id: Mapped[int] = mapped_column(ForeignKey("scenario.id"), nullable=False)
    category_id: Mapped[int] = mapped_column(ForeignKey("category.id"), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    path: Mapped[PlanPath] = mapped_column(Enum(PlanPath), nullable=False)
    # NOT NULL: a plan value without a derivation cannot exist.
    derivation_id: Mapped[int] = mapped_column(ForeignKey("derivation.id"), nullable=False)
    ai_record_id: Mapped[int | None] = mapped_column(ForeignKey("ai_record.id"), nullable=True)

    scenario: Mapped[Scenario] = relationship(back_populates="plan_values")
    category: Mapped[Category] = relationship(back_populates="plan_values")
    derivation: Mapped[Derivation] = relationship(back_populates="plan_values")
    ai_record: Mapped[AiRecord | None] = relationship()


class ExternalNote(Base):
    __tablename__ = "external_note"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("category.id"), nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    source: Mapped[NoteSource] = mapped_column(Enum(NoteSource), nullable=False, default=NoteSource.manual)
    # Set for source=ai_scan: the ai_record (env scan run) that produced this note.
    ai_record_id: Mapped[int | None] = mapped_column(ForeignKey("ai_record.id"), nullable=True)

    category: Mapped[Category | None] = relationship()


class StatementLine(Base):
    __tablename__ = "statement_line"
    __table_args__ = (
        UniqueConstraint(
            "scenario_id", "statement", "line_code", "year", name="uq_statement_line_scenario_stmt_line_year"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scenario_id: Mapped[int] = mapped_column(ForeignKey("scenario.id"), nullable=False)
    statement: Mapped[Statement] = mapped_column(Enum(Statement), nullable=False)
    line_code: Mapped[str] = mapped_column(String(64), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    mapping_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    # NOT NULL: a statement line without a derivation cannot exist.
    derivation_id: Mapped[int] = mapped_column(ForeignKey("derivation.id"), nullable=False)

    scenario: Mapped[Scenario] = relationship(back_populates="statement_lines")
    derivation: Mapped[Derivation] = relationship(back_populates="statement_lines")


ALL_TABLES = [
    Category,
    Actual,
    Parameter,
    Scenario,
    Derivation,
    PlanValue,
    ExternalNote,
    AiRecord,
    StatementLine,
]
