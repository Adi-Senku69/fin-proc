"""``nvplan-ai-check`` - the live smoke check of the configured Claude model.

This is the first thing to run after putting a key in ``.env``. It answers, in order:

1. **What is configured?** model id, effort, max_tokens, betas and where each value came from
   (the ``NVPLAN_*`` environment variable or the ``nvplan.config`` default). Printed always,
   with or without a credential.
2. **Is there a credential?** Without one it prints ``nvplan.ai.credential_hint()`` - the same
   message ``MissingCredentials`` carries and the API returns on 503 - and exits **1**. Nothing
   is sent anywhere.
3. **Does one real call work?** With a credential it makes exactly *one* cheap call through the
   same client construction the touchpoints use (``build_chat_model``), with a trivial prompt,
   ``max_tokens=64`` and ``effort="low"``, and prints the configured model id, the resolved
   model version (what lands in ``ai_record.model_version``), the model id the server reported,
   input/output tokens, any cache tokens, wall-clock latency and a ``PASS`` line.
4. **Does a whole touchpoint work?** ``--touchpoint {env-scan,revenue-proposal,deviation-explanation}``
   instead runs one real touchpoint end to end against a **temporary** database seeded exactly
   like ``nvplan-demo`` (illustrative actuals, the external notes, ``run_plan``, and for the
   deviation the demo's backtest-year plan), then prints the stored prompt excerpt, the
   rationale, the proposed value and the ``ai_record`` id. The temporary DB is deleted unless
   ``--db`` names a file.

Exit codes: 0 pass, 1 no credential, 2 the call or the run failed (the failure is printed with
what to try next).
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import textwrap
import time
import traceback
from pathlib import Path
from typing import Any, Callable

from sqlalchemy.orm import sessionmaker

from nvplan import config
from nvplan.ai.agents import (
    MissingCredentials,
    ModelRefused,
    ProposalRejected,
    build_chat_model,
    credential_hint,
    credentials_available,
    model_version,
    run_deviation_explanation,
    run_env_scan,
    run_revenue_proposal,
)
from nvplan.ai.audit import message_text, total_usage, usage_entry
from nvplan.ai.figures import ExplanationRejected
from nvplan.db.models import AiRecord
from nvplan.db.session import get_engine, init_db, seed_categories

TOUCHPOINT_CHOICES: tuple[str, ...] = ("env-scan", "revenue-proposal", "deviation-explanation")

SMOKE_PROMPT = "Reply with exactly one word: ready"
SMOKE_MAX_TOKENS = 64
SMOKE_EFFORT = "low"

# Same years the demo uses, so the seeded DB and the run are comparable to `nvplan-demo`.
BACKTEST_YEAR = 2025
PROPOSAL_YEAR = 2027

RULE = "=" * 78
Out = Callable[[str], None]


# --------------------------------------------------------------------------- printing


def _kv(out: Out, key: str, value: Any, note: str = "") -> None:
    out(f"  {key:<16}{value}" + (f"   [{note}]" if note else ""))


def _usage_line(usage: dict[str, int] | None) -> str:
    if not usage:
        return "(none reported - a fake model reports no usage_metadata)"
    parts = [f"input {usage.get('input_tokens', 0):,}", f"output {usage.get('output_tokens', 0):,}"]
    if "total_tokens" in usage:
        parts.append(f"total {usage['total_tokens']:,}")
    for key, label in (("cache_read_tokens", "cache read"), ("cache_creation_tokens", "cache creation")):
        if key in usage:
            parts.append(f"{label} {usage[key]:,}")
    return ", ".join(parts)


def _source(var: str) -> str:
    return var if os.environ.get(var) else "config default"


def _mask(value: str) -> str:
    return f"{value[:11]}...{value[-4:]}" if len(value) > 20 else "set"


def print_configuration(out: Out) -> None:
    out("configuration (nvplan.config; .env is loaded at import, existing env wins)")
    _kv(out, "model", config.AI_MODEL, _source("NVPLAN_AI_MODEL"))
    _kv(out, "effort", config.AI_EFFORT, _source("NVPLAN_AI_EFFORT"))
    _kv(out, "max_tokens", f"{config.AI_MAX_TOKENS:,}", _source("NVPLAN_AI_MAX_TOKENS"))
    _kv(out, "betas", ", ".join(config.AI_BETAS) or "(none)", _source("NVPLAN_AI_BETAS"))
    _kv(out, ".env", config.DOTENV_PATH, "present" if config.DOTENV_PATH.exists() else "absent")
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    _kv(out, "credential", _mask(key) if key else "(no ANTHROPIC_API_KEY in the environment)",
        "ANTHROPIC_API_KEY" if key else "see the hint below")
    _kv(out, "workspace", "ANTHROPIC_WORKSPACE_ID set -> sent as the anthropic-workspace-id header"
        if os.environ.get("ANTHROPIC_WORKSPACE_ID") else
        "(unset - only an organization-level key needs the anthropic-workspace-id header)")
    out("  never sent: temperature / top_p / top_k (Opus 5 rejects sampling params with a 400) and")
    out("              no thinking budget (adaptive thinking is on by default; budget_tokens is a 400)")


def print_failure(out: Out, exc: BaseException) -> None:
    out("")
    out(f"FAIL  {type(exc).__name__}: {exc}")
    text = f"{type(exc).__name__} {exc}".lower()
    hints: list[str] = []
    if "credential materialization" in text or "denied" in text or "sandbox" in text:
        hints.append("this looks like a sandbox/egress denial, not a problem with the key or the code - "
                     "run `uv run nvplan-ai-check` yourself in a normal shell")
    if "authentication" in text or "401" in text or "invalid x-api-key" in text:
        hints.append(f"the credential was rejected: check the ANTHROPIC_API_KEY value in {config.DOTENV_PATH}")
    if "not scoped to a workspace" in text or "anthropic-workspace-id" in text:
        hints.append("the key is organization-level, not workspace-scoped: put ANTHROPIC_WORKSPACE_ID=<workspace id> "
                     f"in {config.DOTENV_PATH} (nvplan then sends it as the anthropic-workspace-id header), "
                     "or use a workspace-scoped key")
    if "403" in text or "permission" in text:
        hints.append("the key is valid but not allowed here: check the workspace/scopes of the key in the console")
    if "not_found" in text or "404" in text or "model" in text and "does not exist" in text:
        hints.append(f"model {config.AI_MODEL!r} was not found for this key - override with NVPLAN_AI_MODEL")
    if "connection" in text or "timeout" in text or "timed out" in text:
        hints.append("network problem: no route to api.anthropic.com from here")
    if "400" in text and ("temperature" in text or "budget" in text or "top_" in text):
        hints.append("a rejected request parameter - nvplan sends only model/max_tokens/effort/betas, "
                     "so check NVPLAN_AI_BETAS and any local patch")
    for hint in hints or ["full traceback below; nothing was persisted"]:
        out(f"      - {hint}")
    out("")
    out("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip())


# --------------------------------------------------------------------------- the single call


def smoke_call(out: Out) -> int:
    """One cheap real call through the configured client. 0 on PASS, 2 on failure."""
    out("")
    out(f"one real call: {SMOKE_PROMPT!r}  (max_tokens={SMOKE_MAX_TOKENS}, effort={SMOKE_EFFORT!r})")
    chat = build_chat_model(max_tokens=SMOKE_MAX_TOKENS, effort=SMOKE_EFFORT)
    t0 = time.perf_counter()
    response = chat.invoke([{"role": "user", "content": SMOKE_PROMPT}])
    latency = time.perf_counter() - t0

    meta = getattr(response, "response_metadata", None) or {}
    _kv(out, "model id", config.AI_MODEL, "config.AI_MODEL")
    _kv(out, "model_version", model_version(chat), "stored in ai_record.model_version")
    _kv(out, "served by", meta.get("model") or meta.get("model_name") or "(not reported)", "response_metadata")
    _kv(out, "stop_reason", meta.get("stop_reason") or "(not reported)")
    _kv(out, "usage", _usage_line(usage_entry(response)))
    _kv(out, "latency", f"{latency:.2f} s")
    _kv(out, "reply", (message_text(response.content).strip() or "(empty)")[:200])
    out("")
    out(f"PASS  {config.AI_MODEL} answered in {latency:.2f} s through nvplan's own client construction; "
        "the AI touchpoints will use the same one.")
    return 0


# --------------------------------------------------------------------------- one real touchpoint


def seed_demo_db(path: Path, *, with_backtest_plan: bool) -> sessionmaker:
    """A fresh SQLite seeded exactly like ``nvplan-demo``: categories, the illustrative actuals,
    the external notes and ``run_plan``; plus the demo's backtest-year plan (the only plan whose
    years also have actuals, which the deviation explanation needs)."""
    from nvplan.api import queries as q
    from nvplan.api.demo import run_backtest_year_plan
    from nvplan.ingest.actuals import ingest_actuals, load_actuals_csv
    from nvplan.services.planning import run_plan

    engine = init_db(get_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False}))
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        seed_categories(s)
        ingest_actuals(s, load_actuals_csv(config.DATA_DIR / "actuals.csv"))
        q.seed_external_notes(s)
    with factory() as s:
        run_plan(s, created_by="nvplan-ai-check")
    if with_backtest_plan:
        with factory() as s:
            run_backtest_year_plan(s, BACKTEST_YEAR, created_by="nvplan-ai-check")
    return factory


def _excerpt(text: str, n_lines: int = 10, *, wrap: bool = False) -> str:
    """The first ``n_lines`` lines, prefixed with a gutter; ``wrap`` re-flows long prose lines."""
    raw = (text or "").splitlines()
    lines: list[str] = []
    for ln in raw:
        lines.extend(textwrap.wrap(ln, width=110) or [""] if wrap and len(ln) > 110 else [ln])
    shown = "\n".join(f"      | {ln[:160]}" for ln in lines[:n_lines])
    more = f"\n      | ... ({len(lines) - n_lines} more lines; full text in ai_record.prompt_text)" if len(lines) > n_lines else ""
    return shown + more


def run_touchpoint(out: Out, touchpoint: str, db: Path | None) -> int:
    """Run one real touchpoint end to end on a temporary (or ``--db``) database. 0 / 2."""
    from nvplan.api import queries as q

    tmp_dir = None
    if db is None:
        tmp_dir = tempfile.TemporaryDirectory(prefix="nvplan-ai-check-")
        db = Path(tmp_dir.name) / "nvplan_ai_check.db"
    elif db.exists():
        db.unlink()
    try:
        out("")
        out(f"touchpoint {touchpoint} against the real model, database {db}")
        factory = seed_demo_db(db, with_backtest_plan=touchpoint == "deviation-explanation")
        t0 = time.perf_counter()

        if touchpoint == "env-scan":
            _kv(out, "seeded", f"illustrative actuals + external notes + run_plan ({config.PLAN_YEARS[0]}-{config.PLAN_YEARS[1]})")
            rec = run_env_scan(factory)
        elif touchpoint == "revenue-proposal":
            with factory() as s:
                default = q.default_revenue(s, "base", PROPOSAL_YEAR)
            _kv(out, "seeded", f"base plan; valorized default revenue {PROPOSAL_YEAR} = {default:,.1f} k EUR")
            rec = run_revenue_proposal(factory, scenario_kind="base", year=PROPOSAL_YEAR, default_value=default)
        else:
            _kv(out, "seeded", f"demo backtest-year plan (fit -> {BACKTEST_YEAR}), the year that also has actuals")
            rec = run_deviation_explanation(factory, scenario_kind="base", year=BACKTEST_YEAR)
        latency = time.perf_counter() - t0

        with factory() as s:
            stored = s.get(AiRecord, rec.id)
            log = list(stored.call_log_json or [])
            note_ids = q.ai_record_dict(s, stored)["note_ids"]  # external notes this record wrote (env scan)
            out("")
            _kv(out, "ai_record id", stored.id, f"status={stored.status.value}, touchpoint={stored.touchpoint.value}")
            _kv(out, "model_version", stored.model_version)
            _kv(out, "proposed value", f"{stored.proposed_value:,.1f} k EUR" if stored.proposed_value is not None
                else "(none - this touchpoint proposes no value)")
            _kv(out, "year/scenario", f"{stored.year} / {stored.scenario_id}")
            _kv(out, "model calls", len(log))
            _kv(out, "usage", _usage_line(total_usage(log)))
            _kv(out, "latency", f"{latency:.2f} s")
            if note_ids:
                _kv(out, "notes written", ", ".join(str(i) for i in note_ids))
            out("")
            out("    stored prompt (ai_record.prompt_text), first lines:")
            out(_excerpt(stored.prompt_text))
            out("")
            out("    rationale (ai_record.rationale):")
            out(_excerpt(stored.rationale, 12, wrap=True))
        out("")
        out(f"PASS  touchpoint {touchpoint} ran against {config.AI_MODEL} and persisted ai_record {rec.id} "
            f"(status=proposed; the human gate is elsewhere).")
        return 0
    finally:
        if tmp_dir is not None:
            tmp_dir.cleanup()


# --------------------------------------------------------------------------- entrypoint


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nvplan-ai-check",
        description="Check the configured Claude model: print the resolved configuration, then make one "
                    "cheap real call (or run one whole touchpoint with --touchpoint).",
    )
    parser.add_argument("--touchpoint", choices=TOUCHPOINT_CHOICES,
                        help="run this touchpoint end to end against a temporary demo-seeded database")
    parser.add_argument("--db", type=Path, default=None,
                        help="with --touchpoint: keep the database in this file instead of a temporary one")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    out: Out = print
    out(RULE)
    out(f"nvplan-ai-check - live check of the AI layer's model ({config.AI_MODEL})")
    out(RULE)
    print_configuration(out)

    if not credentials_available():
        out("")
        out("NO CREDENTIAL - nothing was sent.")
        out(credential_hint())
        return 1

    try:
        if args.touchpoint:
            return run_touchpoint(out, args.touchpoint, args.db)
        return smoke_call(out)
    except MissingCredentials as exc:  # credential vanished between the check and the call
        out("")
        out(f"NO CREDENTIAL - {exc}")
        return 1
    except (ExplanationRejected, ProposalRejected) as exc:
        # A guardrail did its job: the model's own figures / value broke a deterministic rule and
        # nothing was persisted. Not a configuration problem, so it gets its own exit path.
        out("")
        out(f"REJECTED BY A GUARDRAIL  {type(exc).__name__}: {exc}")
        out("      - the real model ran, but its answer failed the deterministic cross-check "
            "(nvplan.ai.figures.check_explanation / the control table), so no ai_record was written")
        out("      - that is the PoC's hard rule working as designed; re-run, or raise NVPLAN_AI_EFFORT")
        return 2
    except ModelRefused as exc:
        out("")
        out(f"REFUSED  {exc}")
        out("      - the model declined this request (HTTP 200, stop_reason=refusal). Nothing was persisted; "
            "the API maps this to 502.")
        return 2
    except Exception as exc:  # noqa: BLE001 - a smoke check must print, never traceback-crash
        print_failure(out, exc)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
