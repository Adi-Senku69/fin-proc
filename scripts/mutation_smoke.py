"""Mutation smoke: deliberately weaken a governance rule and check the suite notices
(A2, PLATFORM.md's own bar: "the schema is the backstop" only holds if breaking it
actually fails a test).

This codebase enforces its safety properties in code, not in a policy document: a
non-null FK, a structural refusal (``ValueError``), a closed allow-list, a validator
that assigns "error" vs "warning". A green test suite proves nothing about any one of
those unless something in it would go red the moment the rule is quietly weakened. This
script applies a hand-picked list of exactly such weakenings — one file, one line, one
plausible "helpful" edit each — and treats a mutation that survives as the real finding,
not a bug in the script.

Deliberately a short, curated list rather than an exhaustive mutation run: every entry
here is a rule whose silent failure would be a *governance* failure (a forged claim
accepted, a symlink write allowed, a fit graded "review" entering a plan), not merely a
gap in line coverage. Keeping the list short is what makes it fast enough to run on
every change rather than occasionally.

    uv run python scripts/mutation_smoke.py
    uv run nvplan-mutation-smoke                # same thing, installed console script

Safety: this edits source files in place, so it restores them on success, on failure,
on Ctrl-C (SIGINT) and on termination (SIGTERM). Every touched file is copied to a
backup directory before anything is mutated, and a run that finds that directory
already present restores from it *before doing anything else* — because a hard kill
(a CI job timeout, `kill -9`, an OOM) can outrun a Python signal handler, and the
failure mode that leaves behind is not "the script crashed", it is "the working tree
quietly has a wrong `>=` in it and nothing downstream notices."
"""

from __future__ import annotations

import atexit
import pathlib
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from types import FrameType

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
BACKUP_DIR = REPO_ROOT / ".mutation_smoke_backup"

_PYTEST_BASE_ARGS = ["-q", "-p", "no:cacheprovider", "-p", "no:warnings"]


@dataclass(frozen=True)
class Mutation:
    """One deliberate weakening. ``old`` must appear in ``file`` exactly once — an
    ambiguous anchor is refused rather than guessed at (see ``main``). ``tests`` names
    the targeted pytest paths that genuinely exercise the rule (not the whole suite —
    see the module docstring on why a subset is the right call, and each entry's
    ``why`` for the argument that this subset is not just "some tests that happen to
    pass through the file")."""

    file: str
    old: str
    new: str
    label: str
    tests: tuple[str, ...]
    why: str


#: PLATFORM.md-critical rules, one line each. Each ``why`` is the coverage argument for
#: its ``tests`` subset — read it before trusting a "CAUGHT" result.
MUTATIONS: list[Mutation] = [
    Mutation(
        file="brainkit/validate.py",
        old='add("orphan_evidence", f"evidence row without exactly one provenance tag: {row[:100]!r}")',
        new='add("orphan_evidence", f"evidence row without exactly one provenance tag: {row[:100]!r}", severity="warning")',
        label="validate: orphan_evidence downgraded from error to warning",
        tests=("tests/test_brainkit_validate_content.py",),
        why="several tests there build a stripped-tag row and assert "
        '"orphan_evidence" in _codes(findings, "error") — a severity-filtered lookup, '
        "so a silent downgrade to warning drops the code out of that set.",
    ),
    Mutation(
        file="brainkit/validate.py",
        old='if is_decision and (parsed.status or "").strip().lower() == "decided":',
        new='if is_decision and (parsed.status or "").strip().lower() != "decided":',
        label="validate: missing_reversal fires on the wrong status (inverted)",
        tests=(
            "tests/test_brainkit_validate_placement.py",
            "tests/test_brainkit_writer.py",
            "tests/test_brainkit_ingest.py",
        ),
        why="test_brainkit_ingest.py asserts missing_reversal fires on a *decided* "
        "decision with a vague reversal condition; test_brainkit_writer.py's "
        "promotability tests rely on it firing only once a draft is promoted to "
        "decided, not while it is still pending.",
    ),
    Mutation(
        file="brainkit/writer.py",
        old="    if candidate.is_symlink():",
        new="    if False and candidate.is_symlink():",
        label="writer: _confine's symlink refusal disabled",
        tests=("tests/test_brainkit_writer.py",),
        why="test_symlinked_target_is_refused_and_disk_untouched drafts through a "
        "symlinked target and asserts path_confinement is refused and the symlink "
        "itself is untouched — the exact case this line exists for.",
    ),
    Mutation(
        file="brainkit/writer.py",
        old="    if promoted_rendered is not None:",
        new="    if False and promoted_rendered is not None:",
        label="writer: second (promotability) validate pass disabled",
        tests=("tests/test_brainkit_writer.py",),
        why='the "a draft must be promotable" test block (test_vague_reversal_survives_'
        "the_pending_pass_but_fails_promotion and neighbors) drafts a decision that "
        "passes pending-status validation but would fail once promoted, and asserts "
        "not_promotable is raised anyway — only reachable if the second pass runs.",
    ),
    Mutation(
        file="brainkit/writer.py",
        old='        _section("Status", "pending"),',
        new='        _section("Status", "decided"),',
        label="writer: draft_decision renders decided instead of pending",
        tests=("tests/test_brainkit_writer.py",),
        why="test_decision_status_is_always_pending_regardless_of_body_text drafts a "
        "decision and reads the rendered '## Status' section back, asserting it is "
        "pending no matter what the caller's body text says.",
    ),
    Mutation(
        file="nvplan/ai/assistant.py",
        old="def _status_matches(asserted: str, stored: str) -> bool:\n    return asserted == stored",
        new="def _status_matches(asserted: str, stored: str) -> bool:\n    return True",
        label="assistant: _status_matches always agrees (claim-forgery guard)",
        tests=("tests/test_assistant.py",),
        why="test_claim_segment_with_wrong_status_is_rejected asserts AnswerRejected "
        "(matching 'status') when a real claim_id is paired with a status the row does "
        "not actually have.",
    ),
    Mutation(
        file="nvplan/ai/assistant.py",
        old="def _title_matches(asserted: str, stored: str) -> bool:\n    a = re.sub(r\"\\s+\", \" \", asserted.strip()).casefold()\n    b = re.sub(r\"\\s+\", \" \", stored.strip()).casefold()\n    return a == b",
        new="def _title_matches(asserted: str, stored: str) -> bool:\n    return True",
        label="assistant: _title_matches always agrees (claim-forgery guard)",
        tests=("tests/test_assistant.py",),
        why="test_title_matches_rejects_semantic_inversions_with_high_character_overlap "
        "and test_claim_segment_with_wrong_title_is_rejected both feed a title that "
        "names a different record and require rejection.",
    ),
    Mutation(
        file="nvplan/core/kpi.py",
        old="        if value >= good:",
        new="        if value > good:",
        label="kpi: green boundary flipped from inclusive to exclusive",
        tests=("tests/test_core_kpi.py",),
        why="test_exactly_the_good_threshold_is_green_when_higher_is_better puts a "
        "value exactly on the good threshold and asserts GREEN — this is the same "
        ">= -> > boundary bug class the reference implementation's mutation smoke "
        "test caught for real.",
    ),
    Mutation(
        file="nvplan/core/regression.py",
        old="    if r_squared >= GOOD_R2:",
        new="    if r_squared > GOOD_R2:",
        label="regression: GOOD_R2 boundary flipped from inclusive to exclusive",
        tests=("tests/test_core_regression.py",),
        why="test_grade_fit_thresholds asserts _grade_fit(GOOD_R2) == (\"good\", None) "
        "exactly on the boundary.",
    ),
    Mutation(
        file="nvplan/core/projector.py",
        old="    if review:",
        new="    if False:",
        label="projector: review-graded fit refusal disabled",
        tests=("tests/test_core_projector.py",),
        why="test_project_scenario_refuses_a_review_graded_fit forces one category to "
        'grade "review" and asserts project_scenario raises ValueError — C1\'s '
        "structural refusal, this codebase's own term for it.",
    ),
    Mutation(
        file="nvplan/core/statements.py",
        old="ABS_TOL = 1e-6",
        new="ABS_TOL = 1e6",
        label="statements: balance tolerance widened to uselessness",
        tests=("tests/test_statements.py",),
        why="check_consistency's balance/tie assertions in test_statements.py compare "
        "with pytest.approx(..., abs=1e-6) against real generated statements; widening "
        "ABS_TOL to 1e6 would let check_consistency wave through a genuinely unbalanced "
        "sheet.",
    ),
    Mutation(
        file="bridge/sweep.py",
        old='    return ReversalVerdict(claim.id, claim.slug, title, text, "advisory", None, detail)',
        new='    return ReversalVerdict(claim.id, claim.slug, title, text, "advisory", True, detail)',
        label="sweep: advisory verdict reports the model's own tripped/not-tripped call",
        tests=("tests/test_bridge_sweep.py",),
        why="test_advisory_fallback_when_neither_template_matches asserts v.tripped is "
        "None for the advisory mechanism, twice (with and without a model attached) — "
        'the module\'s own central guarantee: "a model can never decide a condition '
        'tripped."',
    ),
]


def _touched_files() -> list[pathlib.Path]:
    return sorted({REPO_ROOT / m.file for m in MUTATIONS})


def _recover_from_interrupted_run() -> None:
    """Restore anything a previous run left mutated, before this run touches a thing.
    This is the real safety net, not the signal handlers below: a `kill -9` or a CI job
    timeout cannot be trapped, so the only thing that saves the tree in that case is the
    *next* run (or a human) noticing this directory and restoring from it."""
    if not BACKUP_DIR.exists():
        return
    print(
        f"found {BACKUP_DIR.relative_to(REPO_ROOT)} left over from an interrupted run; "
        "restoring originals before doing anything else",
        file=sys.stderr,
    )
    for backup in BACKUP_DIR.rglob("*"):
        if backup.is_file():
            target = REPO_ROOT / backup.relative_to(BACKUP_DIR)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(backup.read_bytes())
    shutil.rmtree(BACKUP_DIR)


def _take_backup() -> None:
    for path in _touched_files():
        destination = BACKUP_DIR / path.relative_to(REPO_ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(path.read_bytes())


def _restore() -> None:
    """Unconditional: called on success, on failure, on exit, and from both signal
    handlers. Safe to call more than once and safe to call when nothing is mutated."""
    if not BACKUP_DIR.exists():
        return
    for path in _touched_files():
        backup = BACKUP_DIR / path.relative_to(REPO_ROOT)
        if backup.exists():
            path.write_bytes(backup.read_bytes())
    shutil.rmtree(BACKUP_DIR, ignore_errors=True)


def _on_signal(signum: int, _frame: FrameType | None) -> None:
    _restore()
    sys.exit(128 + signum)


def _run_tests(tests: tuple[str, ...]) -> bool:
    """True when the targeted subset fails, i.e. the mutation was caught."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *tests, *_PYTEST_BASE_ARGS],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    return result.returncode != 0


def main() -> int:
    _recover_from_interrupted_run()
    _take_backup()
    atexit.register(_restore)
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    survivors: list[str] = []
    for m in MUTATIONS:
        path = REPO_ROOT / m.file
        original = path.read_text()
        if original.count(m.old) != 1:
            survivors.append(
                f"ANCHOR MISSING/AMBIGUOUS  {m.label}  ({m.old!r} found "
                f"{original.count(m.old)} time(s) in {m.file})"
            )
            print(f"  ANCHOR?   {m.label}")
            continue

        path.write_text(original.replace(m.old, m.new, 1))
        try:
            caught = _run_tests(m.tests)
        finally:
            path.write_text(original)

        print(f"  {'CAUGHT ' if caught else 'SURVIVED'}  {m.label}")
        if not caught:
            survivors.append(f"SURVIVED  {m.label}  ({', '.join(m.tests)})")

    _restore()
    print()
    if survivors:
        print("MUTATIONS NOT CAUGHT (coverage gaps, not script bugs):")
        for entry in survivors:
            print("   ", entry)
        print(
            "\nA surviving mutation is a governance rule nothing in the targeted subset "
            "tests. Do not paper over it here — add or fix a test for the rule itself."
        )
        return 1
    print("every mutation was caught")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
