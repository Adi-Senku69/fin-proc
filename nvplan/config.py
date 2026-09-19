"""Project-wide constants: paths, category codes, windows, labels, AI model knobs.

Environment (.env)
------------------
Importing this module loads ``<project root>/.env`` once via python-dotenv with
``override=False``: a variable already present in the real environment always wins, and a
missing file is not an error. That is how ``ANTHROPIC_API_KEY`` reaches the Anthropic client
without anyone exporting it (``.env`` is git-ignored, mode 600; ``.env.example`` documents the
format).

Ordering matters: this module loads the file at import time, and ``nvplan.ai.agents`` reads the
environment *lazily at call time* (``credentials_available``, ``get_model``), never at import.
So any import order works, and a test that deletes ``ANTHROPIC_API_KEY`` with
``monkeypatch.delenv`` still sees the no-credential path.

The ``AI_*`` values below are read from the environment at import with the documented
``NVPLAN_*`` variable names, so ``.env`` can override every one of them.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "illustrative"

DOTENV_PATH = PROJECT_ROOT / ".env"
# Non-fatal when absent; never overrides an already-set environment variable. The second call
# picks up a .env in the current working directory (or above it) when nvplan is used elsewhere.
load_dotenv(DOTENV_PATH, override=False)
load_dotenv(override=False)

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


# --------------------------------------------------------------------------- env helpers


def _env(name: str) -> str | None:
    """The environment value of ``name``, or None when unset/blank."""
    raw = os.environ.get(name)
    return raw.strip() if raw and raw.strip() else None


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name}={raw!r} is not an integer") from exc
    if value <= 0:
        raise ValueError(f"{name}={raw!r} must be a positive number of tokens")
    return value


def _env_list(name: str) -> list[str]:
    """Comma-separated list, empty when unset."""
    raw = _env(name)
    return [part.strip() for part in raw.split(",") if part.strip()] if raw else []


def _env_float(name: str, default: float, *, lo: float = 0.0, hi: float = 1.0) -> float:
    """A float in ``[lo, hi)``; ``default`` when unset."""
    raw = _env(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name}={raw!r} is not a number") from exc
    if not (lo <= value < hi):
        raise ValueError(f"{name}={raw!r} must be in [{lo}, {hi})")
    return value


# --------------------------------------------------------------------------- regression method knobs
#
# VERIFICATION.md 5.1 / 5.2: the PDF's constant-intercept OLS cannot separate a growing fixed
# part from the variable rate, and its valorization rate (mean YoY growth of the fixed part)
# is meaningless when the fitted intercept is near zero. Two independent, opt-in fixes:
#
#   REGRESSION_METHOD              - "ols" (PDF, default, unchanged behaviour) or "joint"
#                                     (non-linear least squares on alpha*(1+v)^(t-t0) +
#                                     beta*Revenue_t; selectable per nvplan.core.regression).
#   VALORIZATION_DEGENERACY_SHARE   - the guard on the valorization rate: below this share of
#                                     mean cost, |alpha| is treated as degenerate and the rate
#                                     is reported undefined regardless of which method fit it.

REGRESSION_METHODS: tuple[str, ...] = ("ols", "joint")
REGRESSION_METHOD = _env("NVPLAN_REGRESSION_METHOD") or "ols"
if REGRESSION_METHOD not in REGRESSION_METHODS:
    raise ValueError(
        f"NVPLAN_REGRESSION_METHOD={REGRESSION_METHOD!r} is not valid; "
        f"expected one of {', '.join(REGRESSION_METHODS)}"
    )

# |alpha| < share * mean(cost) over the window => valorization rate reported undefined
# (status "degenerate_intercept"). Default 5%, per VERIFICATION.md 5.2.
VALORIZATION_DEGENERACY_SHARE = _env_float("NVPLAN_VALORIZATION_DEGENERACY_SHARE", 0.05)


# --------------------------------------------------------------------------- AI model config

# Model id handed to ChatAnthropic (nvplan.ai.agents.get_model).
AI_MODEL = _env("NVPLAN_AI_MODEL") or "claude-opus-5"

# Reasoning depth: ChatAnthropic(effort=...) -> request output_config.effort. "high" is the
# API default; "low" is enough for the smoke check, "max" for correctness-critical runs.
AI_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
AI_EFFORT = _env("NVPLAN_AI_EFFORT") or "high"
if AI_EFFORT not in AI_EFFORTS:
    raise ValueError(
        f"NVPLAN_AI_EFFORT={AI_EFFORT!r} is not a valid effort level; expected one of {', '.join(AI_EFFORTS)}"
    )

# Output cap per model call. 16 000 rather than the old 8 192: one env-scan turn can flag many
# of the 54 framework positions with a reasoning line each, and a truncated turn is a lost run.
AI_MAX_TOKENS = _env_int("NVPLAN_AI_MAX_TOKENS", 16_000)

# Escape hatch for Anthropic beta flags (comma-separated), e.g. NVPLAN_AI_BETAS=fast-mode-2026-02-01.
# Empty by default: nothing in this PoC needs a beta.
AI_BETAS: list[str] = _env_list("NVPLAN_AI_BETAS")

# Context-window policy defaults for the AI layer (see nvplan/ai/context.py).
# Approximate tokens (count_tokens_approximately: chars/4 + 3 per message), not model-exact.
AI_CONTEXT_SUMMARIZE_AT = 120_000  # summarize + offload history when the request exceeds this
AI_CONTEXT_SUMMARIZE_KEEP_MESSAGES = 6  # most recent messages kept verbatim after a summary
# No tool-result clearing (ClearToolUsesEdit) by design: a cleared data result would leave the model
# citing figures from memory; eviction keeps every result retrievable via read_file.
AI_CONTEXT_TOOL_RESULT_EVICT_TOKENS = 4_000  # tool results above this go to /large_tool_results/<id> in state
AI_CONTEXT_CACHE_TTL = "5m"  # Anthropic prompt-cache TTL ("5m" | "1h"); ignored for non-Anthropic models
