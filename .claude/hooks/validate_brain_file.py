#!/usr/bin/env python3
"""Brain-file validator hook — PreToolUse and PostToolUse.

PLATFORM.md §9.1 wants write-time enforcement, and doing that after the write has
already landed checks the wrong text: by PostToolUse time the file on disk already
holds whatever the tool just wrote, so a good file about to be broken passes and a
bad file about to be fixed gets blocked. PreToolUse fires *before* the write, while
disk still holds the OLD content, so this hook determines the PROPOSED content —
what the tool is about to write — and validates that instead of the path.

Reads the Claude Code hook JSON payload from stdin. Determines the text to
validate, in order:

  - Write (``tool_input.content`` present): that string is the proposed content.
  - Edit (``tool_input.old_string``/``new_string``): read the current file (empty
    if it doesn't exist yet) and apply the replacement — honouring
    ``replace_all`` — to compute the proposed content. If ``old_string`` isn't
    found in the current file, don't guess: fall back to validating the file on
    disk, and say so in the output.
  - A batched edit shape (``tool_input.edits``, a list of ``old_string``/
    ``new_string`` dicts, e.g. MultiEdit): apply them in order against the current
    file, same fallback rule per edit.
  - Anything else, or no determinable content: validate the file on disk, exactly
    as a plain PostToolUse check would.

Any path outside a ``brain/`` tree is ignored (exit 0). The standalone form —
``python3 .claude/hooks/validate_brain_file.py <path>`` — always validates that
path from disk, unchanged.

Blocking behaviour differs by event, because only PreToolUse can actually refuse
the write:

  - PreToolUse: on >=1 error-level finding, print a single JSON object on stdout
    (the documented PreToolUse hook-output shape) with
    ``permissionDecision: "deny"`` and exit 0 — Claude Code itself blocks the
    tool call based on that JSON, not on this process's exit code. No error means
    no stdout output.
  - PostToolUse and standalone: unchanged from before — a human-readable message
    on stderr and exit code 2 on >=1 error-level finding (the mechanism that feeds
    a blocking error back at that point), exit 0 otherwise. Warnings alone never
    block either event.

Registering the PreToolUse form is a decision for the user/orchestrator to make in
``.claude/settings.json`` (not this script's concern):

    {
      "hooks": {
        "PreToolUse": [
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

_PLATFORM_POINTER = (
    "PLATFORM.md §4.2/§4.4: every evidence bullet needs exactly one provenance tag "
    "from the closed enum, path-typed tags must resolve, and a decided decision needs "
    "a specific, observable reversal condition. Fix the file above before continuing — "
    "this same check runs again at ingest time and a file with an error like this is "
    "never turned into a row (PLATFORM.md §9.2)."
)


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


def _detect_event(payload: dict) -> str:
    """``hook_event_name`` is authoritative when Claude Code sets it on the
    payload. Absent that, infer it from shape: PostToolUse reports on a
    tool that has already run, so — and only so — its payload carries a
    ``tool_response`` key; PreToolUse fires before the tool runs and never has
    one. A payload with neither is treated as PreToolUse (the safer default: it
    means "don't yet assume the write happened")."""
    name = payload.get("hook_event_name")
    if isinstance(name, str) and name:
        return name
    return "PostToolUse" if "tool_response" in payload else "PreToolUse"


def _extract_file_path(tool_input: dict) -> Path | None:
    for key in ("file_path", "filePath", "path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return Path(value)
    return None


def _read_current_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ""


def _apply_one_edit(current: str, old_string: object, new_string: object, replace_all: object) -> tuple[str | None, bool]:
    """Returns (result, ok). ok is False when ``old_string``/``new_string`` aren't
    both strings, or ``old_string`` isn't found in ``current`` — the "don't guess"
    cases the caller must fall back to disk for."""
    if not isinstance(old_string, str) or not isinstance(new_string, str):
        return None, False
    if old_string == "" or old_string not in current:
        return None, False
    if bool(replace_all):
        return current.replace(old_string, new_string), True
    return current.replace(old_string, new_string, 1), True


def _proposed_content(tool_input: dict, path: Path) -> tuple[str | None, str | None]:
    """Determine the text the tool is proposing to write to ``path``.

    Returns ``(text, fallback_note)``. ``text`` is ``None`` when there is nothing
    to validate but the file on disk (the caller then calls ``validate_file`` as
    today); ``fallback_note`` is a human-readable string to surface only for the
    "we tried to compute proposed content and couldn't" case (an absent
    ``old_string``), not for tool shapes that never carried content at all.
    """
    content = tool_input.get("content")
    if isinstance(content, str):
        return content, None

    old_string = tool_input.get("old_string")
    new_string = tool_input.get("new_string")
    if old_string is not None or new_string is not None:
        current = _read_current_text(path)
        result, ok = _apply_one_edit(current, old_string, new_string, tool_input.get("replace_all"))
        if ok:
            return result, None
        return None, (
            f"old_string not found in {path} (or the edit payload was malformed) — "
            f"falling back to validating the file on disk"
        )

    edits = tool_input.get("edits")
    if isinstance(edits, list) and edits:
        if not all(isinstance(e, dict) for e in edits):
            return None, None  # unrecognised shape — silent disk fallback, as documented
        current = _read_current_text(path)
        for edit in edits:
            result, ok = _apply_one_edit(current, edit.get("old_string"), edit.get("new_string"), edit.get("replace_all"))
            if not ok:
                return None, (
                    f"a batched edit's old_string was not found in {path} — "
                    f"falling back to validating the file on disk"
                )
            current = result
        return current, None

    return None, None  # no determinable content — silent disk fallback, as documented


def _findings_for(path: Path, proposed_text: str | None) -> tuple[list, list, str | None]:
    """Returns (errors, warnings, skip_reason). skip_reason is set (and the lists
    empty) when this path is not this hook's concern at all."""
    from brainkit.validate import validate_content, validate_file  # deferred: keep stdlib-only import cost near zero when idle

    path = path.resolve()
    if path.suffix != ".md":
        return [], [], "not-markdown"

    brain_root = _find_brain_root(path)
    if brain_root is None:
        return [], [], "outside-brain"

    if proposed_text is not None:
        findings = validate_content(proposed_text, path=path, brain_root=brain_root)
    else:
        if not path.exists():
            return [], [], "no-content-and-missing"
        findings = validate_file(path, brain_root=brain_root)

    errors = [f for f in findings if f.severity == "error"]
    warnings = [f for f in findings if f.severity == "warning"]
    return errors, warnings, None


def _rel(path: Path) -> Path:
    brain_root = _find_brain_root(path)
    if brain_root is None:
        return path
    try:
        return path.relative_to(brain_root.parent)
    except ValueError:
        return path


def _format_findings(errors: list, warnings: list, fallback_note: str | None) -> tuple[str, str]:
    """Returns (warning_text, error_text) — either may be empty."""
    warning_text = ""
    if warnings:
        lines = [f"{len(warnings)} warning(s):"]
        for finding in warnings:
            loc = f" (line {finding.line})" if finding.line is not None else ""
            lines.append(f"  - {finding.code}{loc}: {finding.message}")
        warning_text = "\n".join(lines)

    error_text = ""
    if errors:
        lines = [f"{len(errors)} BLOCKING error(s):"]
        for finding in errors:
            loc = f" (line {finding.line})" if finding.line is not None else ""
            lines.append(f"  - {finding.code}{loc}: {finding.message}")
        if fallback_note:
            lines.append(fallback_note)
        lines.append("")
        lines.append(_PLATFORM_POINTER)
        error_text = "\n".join(lines)

    return warning_text, error_text


def _emit_pre_tool_deny(rel: Path, error_text: str) -> None:
    reason = f"{rel} — {error_text}"
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(payload))


def _run_human(path: Path, proposed_text: str | None, fallback_note: str | None) -> int:
    """Shared PostToolUse / standalone reporting: stderr message, exit 2 on error."""
    errors, warnings, skip_reason = _findings_for(path, proposed_text)
    if skip_reason is not None:
        return 0

    rel = _rel(path.resolve())
    warning_text, error_text = _format_findings(errors, warnings, fallback_note)

    if warning_text:
        print(f"[validate_brain_file] {rel} — {warning_text}", file=sys.stderr)

    if not errors:
        return 0

    print(f"[validate_brain_file] {rel} — {error_text}", file=sys.stderr)
    return 2


def check_file(path: Path) -> int:
    """Standalone / PostToolUse: validate one file from disk. Returns 0 or 2."""
    return _run_human(path, proposed_text=None, fallback_note=None)


def _handle_pre_tool_use(payload: dict) -> int:
    tool_input = payload.get("tool_input") or {}
    path = _extract_file_path(tool_input)
    if path is None:
        return 0

    proposed_text, fallback_note = _proposed_content(tool_input, path)
    errors, warnings, skip_reason = _findings_for(path, proposed_text)
    if skip_reason is not None:
        return 0

    if errors:
        rel = _rel(path.resolve())
        _, error_text = _format_findings(errors, warnings, fallback_note)
        _emit_pre_tool_deny(rel, error_text)
    return 0


def _handle_post_tool_use(payload: dict) -> int:
    tool_input = payload.get("tool_input") or {}
    path = _extract_file_path(tool_input)
    if path is not None:
        return check_file(path)

    # Legacy/batched shape: multiple file_paths nested under individual edits.
    edits = tool_input.get("edits")
    worst = 0
    if isinstance(edits, list):
        for edit in edits:
            if isinstance(edit, dict):
                fp = edit.get("file_path") or edit.get("filePath")
                if isinstance(fp, str) and fp:
                    worst = max(worst, check_file(Path(fp)))
    return worst


def main() -> int:
    if len(sys.argv) > 1:
        worst = 0
        for arg in sys.argv[1:]:
            worst = max(worst, check_file(Path(arg)))
        return worst

    payload = _read_stdin_payload()
    if not payload:
        return 0

    event = _detect_event(payload)
    if event == "PreToolUse":
        return _handle_pre_tool_use(payload)
    return _handle_post_tool_use(payload)


if __name__ == "__main__":
    sys.exit(main())
