"""Property-based eval harness for the AI touchpoints (work package A3).

The first and most important case is prompt injection: PLATFORM.md phase B2 will ingest raw
third-party transcripts (interviews, meetings, market research) into ``brain/``, a store an
agent later reads to draft decisions - the highest injection-risk surface in the codebase, and
it does not exist yet. This package is the gate B2 has to clear: every place third-party text
already reaches a model prompt today (external notes -> env-scan/revenue-proposal/deviation-
explanation, a free-form question and brain claims -> the assistant) is covered, and the
registry is built so that adding B2's own channel is a registration, not a rewrite - see
``cases.py``'s module docstring for the exact seam.

See ``cases.py`` for the registry and the property each case asserts, and ``runner.py`` for how
a case is executed and reported. ``tests/test_evals.py`` is what actually runs this under the
normal offline test suite (``-m 'not live'``, no credential, no network).
"""

from nvplan.ai.evals.cases import CASES, INJECTION_CHANNELS, KNOWN_HOLES, EvalCase
from nvplan.ai.evals.runner import EvalOutcome, format_report, run_all, run_one

__all__ = [
    "CASES",
    "EvalCase",
    "EvalOutcome",
    "INJECTION_CHANNELS",
    "KNOWN_HOLES",
    "format_report",
    "run_all",
    "run_one",
]
