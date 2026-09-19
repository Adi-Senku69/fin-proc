#!/usr/bin/env python3
"""PostToolUse hook — validates a just-written ``brain/`` markdown file.

Mirrors the enforcement idea in the pm-brain reference (a hook and the ingest-time
validator share the same rules, so a disabled hook can never let a bad file into the
index — PLATFORM.md §9): fast feedback here, the backstop in ``brainkit.ingest``.

Reads the Claude Code PostToolUse JSON payload from stdin, pulls the file path(s) a
Write/Edit just touched, ignores anything outside ``brain/``, runs
``brainkit.validate.validate_file`` on each, and prints any error-level finding in a
readable form. Exits non-zero (2) iff at least one error-level finding was produced;
warnings are printed too but never fail the hook.

Standalone usage for testing (no stdin JSON needed):

    python3 .claude/hooks/validate_brain_file.py brain/decisions/2026-09-19-foo.md

Registering this hook is a decision for the user to make, not this script: see the
snippet at the bottom of this docstring for what would go in ``.claude/settings.json``
(NOT added automatically as part of this phase):

    {
      "hooks": {
        "PostToolUse": [
          {
            "matcher": "Write|Edit",
            "hooks": [
              {"type": "command", "command": "python3 .claude/hooks/validate_brain_file.py"}
            ]
          }
        ]
      }
    }
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# So `brainkit`/`provenance` import without the project being pip-installed — this repo
# puts its own root on sys.path via an editable install already, but the hook may run
# as a bare script outside that environment, so we make sure of it here too.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _find_brain_root(path: Path) -> Path | None:
    """Walk up from ``path`` looking for a directory named ``brain`` with its own
    ``INDEX.md`` — the scaffold's own marker of "this is the brain root"."""
    cur = path.resolve().parent
    while True:
        if cur.name == "brain" and (cur / "INDEX.md").is_file():
            return cur
        if cur.parent == cur:
            return None
        cur = cur.parent


def _read_stdin_payload() -> dict:
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _extract_file_paths(payload: dict) -> list[Path]:
    """Pull every path a Write/Edit/MultiEdit-shaped ``tool_input`` touched."""
    out: list[Path] = []
    tool_input = payload.get("tool_input") or {}
    for key in ("file_path", "filePath", "path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            out.append(Path(value))
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for edit in edits:
            if isinstance(edit, dict):
                fp = edit.get("file_path") or edit.get("filePath")
                if isinstance(fp, str) and fp:
                    out.append(Path(fp))
    seen: set[str] = set()
    deduped: list[Path] = []
    for p in out:
        key = str(p.resolve())
        if key not in seen:
            seen.add(key)
            deduped.append(p)
    return deduped


def check_file(path: Path) -> int:
    """Validate one file. Returns 0 (clean, or not our concern) or 2 (error found)."""
    from brainkit.validate import validate_file  # deferred: keep stdlib-only import cost near zero when idle

    path = path.resolve()
    if not path.exists() or path.suffix != ".md":
        return 0

    brain_root = _find_brain_root(path)
    if brain_root is None:
        return 0  # not under a brain/ tree — not this hook's concern

    findings = validate_file(path, brain_root=brain_root)
    errors = [f for f in findings if f.severity == "error"]
    warnings = [f for f in findings if f.severity == "warning"]

    rel = path.relative_to(brain_root.parent)

    if warnings:
        print(f"[validate_brain_file] {rel} — {len(warnings)} warning(s):", file=sys.stderr)
        for finding in warnings:
            loc = f" (line {finding.line})" if finding.line is not None else ""
            print(f"  - {finding.code}{loc}: {finding.message}", file=sys.stderr)

    if not errors:
        return 0

    print(f"[validate_brain_file] {rel} — {len(errors)} BLOCKING error(s):", file=sys.stderr)
    for finding in errors:
        loc = f" (line {finding.line})" if finding.line is not None else ""
        print(f"  - {finding.code}{loc}: {finding.message}", file=sys.stderr)
    print(
        "\nPLATFORM.md §4.2/§4.4: every evidence bullet needs exactly one provenance tag "
        "from the closed enum, path-typed tags must resolve, and a decided decision needs "
        "a specific, observable reversal condition. Fix the file above before continuing — "
        "this same check runs again at ingest time and a file with an error like this is "
        "never turned into a row (PLATFORM.md §9.2).",
        file=sys.stderr,
    )
    return 2


def main() -> int:
    if len(sys.argv) > 1:
        paths = [Path(a) for a in sys.argv[1:]]
    else:
        payload = _read_stdin_payload()
        paths = _extract_file_paths(payload)

    if not paths:
        return 0

    worst = 0
    for path in paths:
        worst = max(worst, check_file(path))
    return worst


if __name__ == "__main__":
    sys.exit(main())
