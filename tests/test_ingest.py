import pandas as pd
import pytest
from sqlalchemy import select

from nvplan.config import DATA_DIR, ILLUSTRATIVE_LABEL
from nvplan.db.models import Actual, Category
from nvplan.ingest.actuals import actuals_frame, ingest_actuals, load_actuals_csv, load_actuals_xlsx


def test_csv_and_xlsx_load_identically():
    csv = load_actuals_csv(DATA_DIR / "actuals.csv")
    xlsx = load_actuals_xlsx(DATA_DIR / "actuals.xlsx", source_label=ILLUSTRATIVE_LABEL)
    assert list(csv.columns) == ["category_code", "year", "value", "source_label"]
    pd.testing.assert_frame_equal(csv, xlsx, check_exact=False, atol=1e-6)


def test_ingest_round_trip(session):
    df = load_actuals_csv(DATA_DIR / "actuals.csv")
    n = ingest_actuals(session, df, source_label=ILLUSTRATIVE_LABEL)
    assert n == len(df) == 60

    back = actuals_frame(session)
    pd.testing.assert_frame_equal(back, df, check_exact=False, atol=1e-9)

    # components excluded on request: only the five categories remain
    five = actuals_frame(session, include_components=False)
    assert set(five["category_code"]) == {"REV", "MAT", "EXT", "PERS", "OTH"}

    # DEPR landed on the hidden component category
    depr = session.scalar(select(Category).where(Category.code == "DEPR"))
    assert depr.is_component
    assert session.scalar(select(Actual).where(Actual.category_id == depr.id, Actual.year == 2025)) is not None


def test_ingest_upsert(session):
    df = load_actuals_csv(DATA_DIR / "actuals.csv")
    ingest_actuals(session, df)
    rev = session.scalar(select(Category).where(Category.code == "REV"))
    row = session.scalar(select(Actual).where(Actual.category_id == rev.id, Actual.year == 2025))
    old = row.value

    patched = pd.DataFrame([{"category_code": "REV", "year": 2025, "value": old + 100.0}])
    assert ingest_actuals(session, patched, source_label="RESTATED") == 1

    row = session.scalar(select(Actual).where(Actual.category_id == rev.id, Actual.year == 2025))
    assert row.value == pytest.approx(old + 100.0)
    assert row.source_label == "RESTATED"
    assert session.scalar(select(Actual).where(Actual.category_id == rev.id).with_only_columns(Actual.id).order_by(Actual.id.desc())) is not None
    assert len(session.scalars(select(Actual)).all()) == 60


def test_ingest_rejects_unknown_code(session):
    df = pd.DataFrame([{"category_code": "XXX", "year": 2025, "value": 1.0}])
    with pytest.raises(ValueError, match="unknown category codes"):
        ingest_actuals(session, df, source_label="x")


def test_ingest_requires_source_label(session):
    df = pd.DataFrame([{"category_code": "REV", "year": 2025, "value": 1.0}])
    with pytest.raises(ValueError, match="source_label"):
        ingest_actuals(session, df)
