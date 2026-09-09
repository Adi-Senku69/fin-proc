"""Project-wide constants: paths, category codes, windows, labels."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "illustrative"

DEFAULT_DB_URL = "sqlite:///nvplan.db"

# The five planning categories from the PDF (in P&L order).
# "DEPR" is NOT a sixth category: it is a component of OTH (see db.models.Category.is_component).
CATEGORY_CODES: list[str] = ["REV", "MAT", "EXT", "PERS", "OTH"]
COMPONENT_CODES: list[str] = ["DEPR"]

# Historical window used for the OLS regression (inclusive).
REGRESSION_WINDOW: tuple[int, int] = (2021, 2025)
# Planning horizon (inclusive).
PLAN_YEARS: tuple[int, int] = (2026, 2030)

# Every generated / dummy figure carries this label as its source.
ILLUSTRATIVE_LABEL = "ILLUSTRATIVE"

AI_MODEL = "claude-opus-5"
