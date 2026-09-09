"""Engine / session factory, schema creation and category seeding."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from nvplan.config import DEFAULT_DB_URL
from nvplan.db.models import Base, Category, CategoryDriver, CategoryKind

# Unbound factory; bind it via ``init_db(engine)`` or pass ``bind=`` when calling.
SessionLocal = sessionmaker(class_=Session, expire_on_commit=False)


def get_engine(url: str | None = None, **kwargs) -> Engine:
    """Create an engine. SQLite engines get ``PRAGMA foreign_keys=ON`` so FK
    constraints (incl. the NOT NULL derivation FKs) are actually enforced."""
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
    """Create all tables and bind the session factory to this engine."""
    Base.metadata.create_all(engine)
    SessionLocal.configure(bind=engine)
    return engine


# (code, name, kind, driver, is_component)
# DEPR is a component of OTH (depreciation from the investment plan), not a sixth category.
CATEGORY_SEED: list[tuple[str, str, CategoryKind, CategoryDriver | None, bool]] = [
    ("REV", "Revenue", CategoryKind.revenue, None, False),
    ("MAT", "Material costs", CategoryKind.cost, CategoryDriver.revenue, False),
    ("EXT", "External services", CategoryKind.cost, CategoryDriver.revenue, False),
    ("PERS", "Personnel costs", CategoryKind.cost, CategoryDriver.revenue, False),
    ("OTH", "Other costs incl. Depreciation", CategoryKind.cost, CategoryDriver.investment, False),
    ("DEPR", "Depreciation (component of OTH)", CategoryKind.cost, CategoryDriver.investment, True),
]


def seed_categories(session: Session) -> list[Category]:
    """Insert the five categories plus the DEPR component. Idempotent."""
    existing = {c.code: c for c in session.scalars(select(Category)).all()}
    created: list[Category] = []
    for code, name, kind, driver, is_component in CATEGORY_SEED:
        if code in existing:
            continue
        cat = Category(code=code, name=name, kind=kind, driver=driver, is_component=is_component)
        session.add(cat)
        created.append(cat)
    session.commit()
    return created


def category_map(session: Session) -> dict[str, Category]:
    """code -> Category for all rows (components included)."""
    return {c.code: c for c in session.scalars(select(Category)).all()}
