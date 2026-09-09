"""Load actuals from CSV / XLSX into a normalised long DataFrame and upsert `actual` rows.

Long format columns: ``category_code, year, value, source_label``.

``DEPR`` rows are ingested like any other code: they land on the hidden
component category ``DEPR`` (``Category.is_component=True``), which the
planning core subtracts from ``OTH`` before regressing. See db.models.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from nvplan.db.models import Actual, Category

LONG_COLUMNS = ["category_code", "year", "value", "source_label"]
WIDE_SHEET = "Istwerten"


def _normalise(df: pd.DataFrame, source_label: str | None = None) -> pd.DataFrame:
    out = df.copy()
    if "source_label" not in out.columns:
        out["source_label"] = source_label
    if source_label is not None:
        out["source_label"] = out["source_label"].fillna(source_label)
    out = out[LONG_COLUMNS]
    out["category_code"] = out["category_code"].astype(str).str.strip().str.upper()
    out["year"] = out["year"].astype(int)
    out["value"] = out["value"].astype(float)
    return out.sort_values(["category_code", "year"]).reset_index(drop=True)


def load_actuals_csv(path: str | Path, source_label: str | None = None) -> pd.DataFrame:
    """Read a long-format CSV (category_code, year, value[, source_label])."""
    df = pd.read_csv(path)
    missing = {"category_code", "year", "value"} - set(df.columns)
    if missing:
        raise ValueError(f"actuals csv missing columns: {sorted(missing)}")
    return _normalise(df, source_label)


def load_actuals_xlsx(path: str | Path, sheet: str = WIDE_SHEET, source_label: str | None = None) -> pd.DataFrame:
    """Read the wide sheet (rows = category_code [+ name], columns = years) into long format."""
    wide = pd.read_excel(path, sheet_name=sheet)
    if "category_code" not in wide.columns:
        raise ValueError("actuals xlsx sheet needs a 'category_code' column")
    year_cols = [c for c in wide.columns if str(c).strip().isdigit()]
    if not year_cols:
        raise ValueError("actuals xlsx sheet has no year columns")
    long = wide.melt(id_vars=["category_code"], value_vars=year_cols, var_name="year", value_name="value")
    long["year"] = long["year"].astype(str).str.strip().astype(int)
    long = long.dropna(subset=["value"])
    return _normalise(long, source_label)


def ingest_actuals(session: Session, df: pd.DataFrame, source_label: str | None = None) -> int:
    """Upsert `actual` rows by (category, year). Returns the number of rows written.

    Unknown category codes raise ValueError (seed categories first).
    """
    df = _normalise(df, source_label)
    if df["source_label"].isna().any():
        raise ValueError("source_label is required (pass it explicitly or include the column)")

    cats = {c.code: c for c in session.scalars(select(Category)).all()}
    unknown = sorted(set(df["category_code"]) - set(cats))
    if unknown:
        raise ValueError(f"unknown category codes: {unknown}")

    existing = {(a.category_id, a.year): a for a in session.scalars(select(Actual)).all()}
    n = 0
    for row in df.itertuples(index=False):
        cat = cats[row.category_code]
        key = (cat.id, int(row.year))
        if key in existing:
            existing[key].value = float(row.value)
            existing[key].source_label = str(row.source_label)
        else:
            session.add(
                Actual(category_id=cat.id, year=int(row.year), value=float(row.value), source_label=str(row.source_label))
            )
        n += 1
    session.commit()
    return n


def actuals_frame(session: Session, include_components: bool = True) -> pd.DataFrame:
    """Read back `actual` rows as a long DataFrame (category_code, year, value, source_label)."""
    stmt = select(Actual, Category.code, Category.is_component).join(Category)
    rows = [
        {"category_code": code, "year": a.year, "value": a.value, "source_label": a.source_label}
        for a, code, is_component in session.execute(stmt).all()
        if include_components or not is_component
    ]
    return pd.DataFrame(rows, columns=LONG_COLUMNS).sort_values(["category_code", "year"]).reset_index(drop=True)
