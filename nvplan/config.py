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

# Context-window policy defaults for the AI layer (see nvplan/ai/context.py).
# Approximate tokens (count_tokens_approximately: chars/4 + 3 per message), not model-exact.
AI_CONTEXT_SUMMARIZE_AT = 120_000  # summarize + offload history when the request exceeds this
AI_CONTEXT_SUMMARIZE_KEEP_MESSAGES = 6  # most recent messages kept verbatim after a summary
# No tool-result clearing (ClearToolUsesEdit) by design: a cleared data result would leave the model
# citing figures from memory; eviction keeps every result retrievable via read_file.
AI_CONTEXT_TOOL_RESULT_EVICT_TOKENS = 4_000  # tool results above this go to /large_tool_results/<id> in state
AI_CONTEXT_CACHE_TTL = "5m"  # Anthropic prompt-cache TTL ("5m" | "1h"); ignored for non-Anthropic models
