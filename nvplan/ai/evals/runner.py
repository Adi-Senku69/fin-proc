"""Executing the eval-case registry and reporting outcomes (work package A3).

Mirrors the pattern this harness is adapted from (a `pw_api`-style golden-question runner): a
case's `run` executes against its own fresh, isolated database and the outcome is captured as
DATA - pass/fail, never raised past this module - because a failing case is a finding about the
system under test, not a bug in the harness itself. ``uv run python -m nvplan.ai.evals.runner``
gives the same report a CI test gets, for use when comparing a prompt or model change by hand.
"""

from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nvplan.ai.evals.cases import CASES, EvalCase


@dataclass(frozen=True)
class EvalOutcome:
    """One case's result. ``error`` is set only when ``run`` or ``holds`` itself raised
    something unexpected (a harness-level surprise); an injection case that correctly raises
    ``ProposalRejected``/``ExplanationRejected``/``AnswerRejected`` is caught INSIDE its own
    ``run`` and reported as a normal, passing observation - see cases.py."""

    case: EvalCase
    observation: dict[str, Any] | None
    passed: bool
    error: str | None


def run_one(case: EvalCase, tmp_root: Path) -> EvalOutcome:
    """Run one case in its own subdirectory of ``tmp_root`` (its own sqlite file, its own
    brain/ scratch dir where relevant) so no case's writes can leak into another's."""
    case_dir = tmp_root / case.name
    case_dir.mkdir(parents=True, exist_ok=True)
    try:
        observation = case.run(case_dir)
    except Exception as exc:  # noqa: BLE001 - an unexpected raise is itself a finding
        return EvalOutcome(case=case, observation=None, passed=False, error=f"run() raised {type(exc).__name__}: {exc}")
    try:
        passed = bool(case.holds(observation))
    except Exception as exc:  # noqa: BLE001
        return EvalOutcome(case=case, observation=observation, passed=False, error=f"holds() raised {type(exc).__name__}: {exc}")
    return EvalOutcome(case=case, observation=observation, passed=passed, error=None)


def run_all(tmp_root: Path, cases: tuple[EvalCase, ...] = CASES) -> list[EvalOutcome]:
    """Run every case in ``cases`` (the full registry by default) and return one outcome per
    case, in registration order - never fewer, never more (tests/test_evals.py's reachability
    check is built on that guarantee)."""
    return [run_one(case, tmp_root) for case in cases]


def format_report(outcomes: list[EvalOutcome]) -> str:
    lines: list[str] = []
    for o in outcomes:
        mark = "pass" if o.passed else "FAIL"
        tag = "injection" if o.case.injection else "control"
        lines.append(f"[{mark}] ({tag}) {o.case.name:60s} {o.case.invariant}")
        if not o.passed:
            detail = o.error or f"observation: {o.observation!r}"
            lines.append(f"       {detail}")
    n_pass = sum(o.passed for o in outcomes)
    lines.append(f"{n_pass}/{len(outcomes)} passed")
    return "\n".join(lines)


def main() -> int:  # pragma: no cover - a manual CLI, not exercised by the offline suite
    with tempfile.TemporaryDirectory() as tmp:
        outcomes = run_all(Path(tmp))
    print(format_report(outcomes))
    return 0 if all(o.passed for o in outcomes) else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
