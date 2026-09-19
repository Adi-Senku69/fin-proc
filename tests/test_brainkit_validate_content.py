"""Tests for content-based brain-file validation (PLATFORM.md §9.1).

The hook used to validate the file on disk after a Write/Edit had already
landed (PostToolUse). By then the edit is done, so checking the path checks the
wrong text: a good file about to be broken passes, and a bad file about to be
fixed gets blocked. ``brainkit.validate.validate_content`` applies every
existing structural check to *proposed* text instead of disk content, which is
what makes a PreToolUse (before-the-write) hook meaningful.

These tests cover, in order:

  1. ``validate_content`` on real, on-disk text matches ``validate_file`` on the
     same file exactly (same finding-code multiset) — proves there's no drift
     between the two entry points.
  2. Proposed text that orphans an evidence row is caught by ``validate_content``
     even though the real file on disk stays untouched and clean.
  3. The reverse — the whole point of this change: a file on disk that IS
     broken, with proposed content that FIXES it, validates clean.
  4. The hook script itself, driven as a subprocess with synthesized
     PreToolUse/PostToolUse payloads, for both the Write and Edit tool shapes,
     including the deny-JSON contract, the disk-unmodified guarantee, the
     outside-brain/ no-op, and the old_string-not-found fallback.

All brain-tree fixtures live under ``tmp_path`` (a temp copy of the real
``brain/`` tree), never in the repository's own ``brain/`` — a PostToolUse hook
is currently live on ``brain/**`` and would block a deliberately bad file
written there for real.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

pytest.importorskip("provenance", reason="provenance/ (Phase P0) is not present yet")

from brainkit.validate import validate_content, validate_file

REPO_ROOT = Path(__file__).resolve().parents[1]
BRAIN_ROOT = REPO_ROOT / "brain"
HOOK_PATH = REPO_ROOT / ".claude" / "hooks" / "validate_brain_file.py"

REAL_DECISION = BRAIN_ROOT / "decisions" / "2026-09-20-sunset-legacy-import.md"
REAL_DECISION_TEXT = REAL_DECISION.read_text(encoding="utf-8")

# A verbatim line from the real worked example: a wrapped evidence bullet's
# final line, ending in its provenance tag. Stripping the tag orphans the row;
# it's also short and specific enough to use as an Edit's old_string/new_string
# in the hook subprocess tests below.
_TAGGED_LINE = "  lead's own quarterly tracking  (stakeholder-verbal, Head of Support, 2026-09-15)"
_UNTAGGED_LINE = "  lead's own quarterly tracking"
assert _TAGGED_LINE in REAL_DECISION_TEXT

ORPHANED_DECISION_TEXT = REAL_DECISION_TEXT.replace(_TAGGED_LINE, _UNTAGGED_LINE, 1)

# A small, self-contained decision fixture — broken (orphan evidence row) and
# its fix — for the "disk broken, proposal fixes it" and hook allow/deny tests.
BROKEN_DECISION_TEXT = """# Decision: Fixture decision for validate_content tests

## Status
decided

## Date
2026-01-01

## Context
Fixture context paragraph, not a real decision.

## Options considered
1. Do the fixture thing.

## Decision
Do the fixture thing.

## Why
Fixture reasoning text.

## Evidence
- something happened with no provenance tag at all

## Explicitly NOT doing
- the other fixture thing (chat, no artifact)

## What would reverse this
If usage drops below 10 accounts by 2026-06-01.

## Remaining ambiguities
None — this is a fixture.
"""

FIXED_DECISION_TEXT = BROKEN_DECISION_TEXT.replace(
    "- something happened with no provenance tag at all",
    "- something happened  (chat, no artifact)",
)
assert FIXED_DECISION_TEXT != BROKEN_DECISION_TEXT

DECISION_FILENAME = "2026-01-01-fixture-decision.md"


def _codes(findings, severity: str | None = None) -> set[str]:
    if severity is None:
        return {f.code for f in findings}
    return {f.code for f in findings if f.severity == severity}


def _error_codes(findings) -> Counter:
    return Counter(f.code for f in findings if f.severity == "error")


@pytest.fixture()
def tmp_brain(tmp_path: Path) -> Path:
    """A private, editable copy of the real brain/ tree."""
    dest = tmp_path / "brain"
    shutil.copytree(BRAIN_ROOT, dest)
    return dest


def _run_hook(payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestValidateContentMatchesValidateFile:
    def test_real_decision_zero_errors_both_ways(self):
        content_findings = validate_content(REAL_DECISION_TEXT, path=REAL_DECISION, brain_root=BRAIN_ROOT)
        file_findings = validate_file(REAL_DECISION, brain_root=BRAIN_ROOT)

        assert _error_codes(content_findings) == Counter()
        assert _error_codes(file_findings) == Counter()

    def test_real_decision_finding_code_multisets_match_exactly(self):
        content_findings = validate_content(REAL_DECISION_TEXT, path=REAL_DECISION, brain_root=BRAIN_ROOT)
        file_findings = validate_file(REAL_DECISION, brain_root=BRAIN_ROOT)

        assert Counter(f.code for f in content_findings) == Counter(f.code for f in file_findings)


class TestValidateContentCatchesProposedOrphan:
    def test_stripped_tag_in_proposed_text_yields_orphan_evidence(self):
        findings = validate_content(ORPHANED_DECISION_TEXT, path=REAL_DECISION, brain_root=BRAIN_ROOT)
        assert "orphan_evidence" in _codes(findings, "error")

    def test_disk_file_is_untouched_and_stays_clean(self):
        # The proposed text above never touched disk — validate_file on the real
        # path must still see the original, tagged, clean content.
        assert REAL_DECISION.read_text(encoding="utf-8") == REAL_DECISION_TEXT
        findings = validate_file(REAL_DECISION, brain_root=BRAIN_ROOT)
        assert _codes(findings, "error") == set()


class TestReverseCaseDiskBrokenProposalFixes:
    """The whole point of this change: content on disk that IS broken, with
    proposed content that FIXES it, must validate clean — proving it's the
    proposal being judged, not the path."""

    def test_disk_copy_has_orphan_evidence_error(self, tmp_brain: Path):
        path = tmp_brain / "decisions" / DECISION_FILENAME
        path.write_text(BROKEN_DECISION_TEXT, encoding="utf-8")

        findings = validate_file(path, brain_root=tmp_brain)
        assert "orphan_evidence" in _codes(findings, "error")

    def test_proposed_fix_validates_with_zero_errors(self, tmp_brain: Path):
        path = tmp_brain / "decisions" / DECISION_FILENAME
        path.write_text(BROKEN_DECISION_TEXT, encoding="utf-8")

        findings = validate_content(FIXED_DECISION_TEXT, path=path, brain_root=tmp_brain)
        assert _codes(findings, "error") == set()

        # And the disk copy, independently, is still the broken one.
        assert path.read_text(encoding="utf-8") == BROKEN_DECISION_TEXT


class TestHookPreToolUseEditIntroducesOrphan:
    def test_deny_json_on_stdout_and_disk_unmodified(self, tmp_brain: Path):
        target = tmp_brain / "decisions" / "2026-09-20-sunset-legacy-import.md"
        original = target.read_text(encoding="utf-8")
        assert _TAGGED_LINE in original

        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Edit",
            "tool_input": {
                "file_path": str(target),
                "old_string": _TAGGED_LINE,
                "new_string": _UNTAGGED_LINE,
            },
        }
        proc = _run_hook(payload)

        assert proc.returncode == 0
        parsed = json.loads(proc.stdout)
        out = parsed["hookSpecificOutput"]
        assert out["hookEventName"] == "PreToolUse"
        assert out["permissionDecision"] == "deny"
        assert "orphan_evidence" in out["permissionDecisionReason"]
        assert target.name in out["permissionDecisionReason"]

        # The hook never writes — the edit was only proposed, not applied.
        assert target.read_text(encoding="utf-8") == original


class TestHookPreToolUseWriteBadContent:
    def test_deny_json_and_file_never_created(self, tmp_brain: Path):
        target = tmp_brain / "decisions" / DECISION_FILENAME
        assert not target.exists()

        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(target),
                "content": BROKEN_DECISION_TEXT,
            },
        }
        proc = _run_hook(payload)

        assert proc.returncode == 0
        parsed = json.loads(proc.stdout)
        assert parsed["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "orphan_evidence" in parsed["hookSpecificOutput"]["permissionDecisionReason"]

        assert not target.exists()


class TestHookPreToolUseEditFixesBrokenFileIsAllowed:
    def test_no_deny_in_output(self, tmp_brain: Path):
        target = tmp_brain / "decisions" / DECISION_FILENAME
        target.write_text(BROKEN_DECISION_TEXT, encoding="utf-8")

        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Edit",
            "tool_input": {
                "file_path": str(target),
                "old_string": "- something happened with no provenance tag at all",
                "new_string": "- something happened  (chat, no artifact)",
            },
        }
        proc = _run_hook(payload)

        assert proc.returncode == 0
        assert "deny" not in proc.stdout
        # The proposal was never applied to disk either.
        assert target.read_text(encoding="utf-8") == BROKEN_DECISION_TEXT


class TestHookPostToolUseStillHumanReadable:
    def test_post_tool_use_message_and_exit_2(self, tmp_brain: Path):
        target = tmp_brain / "decisions" / DECISION_FILENAME
        target.write_text(BROKEN_DECISION_TEXT, encoding="utf-8")

        payload = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": str(target)},
            "tool_response": {"success": True},
        }
        proc = _run_hook(payload)

        assert proc.returncode == 2
        assert "BLOCKING error" in proc.stderr
        assert "orphan_evidence" in proc.stderr
        assert "PLATFORM.md" in proc.stderr

    def test_post_tool_use_inferred_without_explicit_event_name(self, tmp_brain: Path):
        """No `hook_event_name` key: inferred from the presence of `tool_response`."""
        target = tmp_brain / "decisions" / DECISION_FILENAME
        target.write_text(BROKEN_DECISION_TEXT, encoding="utf-8")

        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(target)},
            "tool_response": {"success": True},
        }
        proc = _run_hook(payload)

        assert proc.returncode == 2
        assert "BLOCKING error" in proc.stderr


class TestHookOutsideBrainIsANoOp:
    @pytest.mark.parametrize("event", ["PreToolUse", "PostToolUse"])
    def test_path_outside_brain_exits_zero_no_output(self, tmp_path: Path, event: str):
        outside = tmp_path / "not-brain" / "notes.md"
        outside.parent.mkdir(parents=True)
        outside.write_text("# Just some notes\n", encoding="utf-8")

        payload = {
            "hook_event_name": event,
            "tool_name": "Write",
            "tool_input": {"file_path": str(outside), "content": "# Just some notes\n"},
        }
        if event == "PostToolUse":
            payload["tool_response"] = {"success": True}
        proc = _run_hook(payload)

        assert proc.returncode == 0
        assert proc.stdout == ""
        assert proc.stderr == ""


class TestHookOldStringNotFoundFallsBackToDisk:
    def test_falls_back_to_disk_validation_without_crashing(self, tmp_brain: Path):
        target = tmp_brain / "decisions" / "2026-09-20-sunset-legacy-import.md"
        assert "this text does not appear anywhere in the file" not in target.read_text(encoding="utf-8")

        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Edit",
            "tool_input": {
                "file_path": str(target),
                "old_string": "this text does not appear anywhere in the file",
                "new_string": "replacement",
            },
        }
        proc = _run_hook(payload)

        # No crash, and since the real on-disk file is clean, the disk-validation
        # fallback finds nothing to deny.
        assert proc.returncode == 0
        assert "deny" not in proc.stdout
