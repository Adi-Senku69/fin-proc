"""deepagents wiring for the three AI touchpoints plus a free-form advisor.

Pinned against deepagents 0.7.13 (``create_deep_agent(model, tools, *,
system_prompt=..., response_format=..., subagents=[...], middleware=[...])``).

Design
------
* ``build_touchpoint_agent`` builds one deep agent per touchpoint with exactly
  the tools that touchpoint needs and a ``ToolStrategy`` structured output.
  The built-in filesystem tool set is replaced by a read-only one
  (``read_file``/``ls``/``grep``) so no agent ever carries a tool named
  write_*/edit_*/delete.
* Every agent gets the context stack from ``nvplan.ai.context`` (tool-result
  eviction, summarization with history offload, prompt caching - no clearing;
  thresholds from a ``ContextPolicy``, default from ``config``) plus
  ``nvplan.ai.audit.ContextAuditMiddleware`` which logs every model call into
  ``AiRunContext.call_log``; the entrypoints persist that as
  ``ai_record.call_log_json``. Files the middleware writes live in graph state
  (``CompositeBackend(default=StateBackend())``), never on disk or in the DB;
  ``/skills/`` is routed read-only to ``nvplan/ai/skills`` so each agent can
  ``read_file`` its SKILL.md (``skills=["/skills/"]``).
* ``build_advisor`` builds one orchestrating agent whose three subagents are
  the touchpoints, for free-form questions.
* ``run_env_scan`` / ``run_revenue_proposal`` / ``run_deviation_explanation``
  render the prompt, invoke, and persist an ``AiRecord`` (status=proposed).

Prompt persistence: a callback captures the system message exactly as it
reaches the model (our authored prompt first, then any framework middleware
text). ``prompt_text`` = that system prompt + ``---USER---`` + the rendered user
prompt. ``response_text`` = the model's final prose + ``---STRUCTURED---`` +
the JSON of the structured response.

Revenue-proposal persistence: the ``record_revenue_proposal`` tool inserts the
``ai_record`` during the run (so validation happens where the model can see
the error and retry) and the entrypoint finalises that same row. If the model
returned a structured proposal without calling the tool, the entrypoint puts it
through the same validator/inserter (``tools.record_proposal``) and raises
``ProposalRejected`` on failure. One run == at most one ai_record.

Deviation-explanation persistence: the structured ``DeviationExplanation`` is
cross-checked against the deterministic plan-vs-actual table
(``figures.check_explanation``: every category once, plan/actual/deviation within
0.05 k EUR, the deviation figure quoted in each explanation) *before* anything is
written; a mismatch raises ``ExplanationRejected`` and no ai_record exists.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Sequence

from langchain.agents.structured_output import ToolStrategy
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool
from sqlalchemy import select

from nvplan import config
from nvplan.ai import prompts
from nvplan.ai.audit import ContextAuditMiddleware
from nvplan.ai.context import (
    SKILLS_SOURCE,
    ContextPolicy,
    build_context_middleware,
    make_backend,
    make_shared_touchpoint_backend,
    skills_index_route,
)
from nvplan.ai.figures import ExplanationRejected, check_explanation, plan_vs_actual
from nvplan.ai.schemas import TOUCHPOINT_SCHEMAS, DeviationExplanation, EnvScanResult, RevenueProposal
from nvplan.ai.tools import (
    AiRunContext,
    SessionFactory,
    _categories,
    _scenario_by_kind,
    load_control_table,
    load_env_framework,
    make_read_tools,
    make_write_tools,
    record_proposal,
)
from nvplan.db.models import Actual, AiRecord, AiStatus, Category, ExternalNote, Touchpoint

TOUCHPOINTS: tuple[str, ...] = ("env_scan", "revenue_proposal", "deviation_explanation")

# Which read tools each touchpoint gets (by tool name) and which write tool, if any.
_TOUCHPOINT_TOOLS: dict[str, tuple[set[str], set[str]]] = {
    "env_scan": (
        {"get_actuals", "get_external_notes", "get_env_framework", "get_parameters"},
        {"record_external_note"},
    ),
    "revenue_proposal": (
        {"get_actuals", "get_external_notes", "get_control_table", "get_plan_values", "get_parameters"},
        {"record_revenue_proposal"},
    ),
    "deviation_explanation": (
        {"get_plan_vs_actual", "get_actuals", "get_plan_values", "get_parameters", "get_external_notes"},
        set(),
    ),
}

_SUBAGENT_NAMES: dict[str, str] = {
    "env_scan": "env-scan",
    "revenue_proposal": "revenue-proposal",
    "deviation_explanation": "deviation-explanation",
}
_SUBAGENT_DESCRIPTIONS: dict[str, str] = {
    "env_scan": "Environmental scan: flags material positions of the 54-position framework and records external notes.",
    "revenue_proposal": "Revenue proposal: proposes a revenue value for one plan year with rationale, within the control-table rules.",
    "deviation_explanation": "Deviation explanation: attributes plan-vs-actual deviations per category, citing the figures.",
}


class ProposalRejected(ValueError):
    """The structured revenue proposal broke a control-table rule; nothing was written."""


class MissingCredentials(RuntimeError):
    """No Anthropic credential is resolvable, so no real model can be built.

    Carries ``credential_hint()`` as its message: the exact thing to do next. Raised instead of
    letting a bare SDK authentication error (or a pydantic validation error about ``api_key``)
    surface. The API's 503 path returns this message verbatim."""


class ModelRefused(RuntimeError):
    """The model declined the request (HTTP 200 with ``stop_reason="refusal"``).

    langchain-anthropic 1.7.1 has no handling for that stop reason at all, so the run would
    otherwise look like an empty or malformed answer. Nothing is persisted (same discipline as
    ``ExplanationRejected``); the API maps it to 502."""


# ExplanationRejected lives in nvplan.ai.figures
__all__ = ["ExplanationRejected", "MissingCredentials", "ModelRefused", "ProposalRejected"]


# --------------------------------------------------------------------------- credentials

# Env vars the Anthropic SDK resolves itself, in its own precedence order.
CREDENTIAL_ENV_VARS: tuple[str, ...] = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

# ANTHROPIC_WORKSPACE_ID is *not* a credential and never gates anything. A workspace-scoped key
# routes itself and needs nothing. An organization-level key does not: the API answers
# 400 invalid_request_error "This API key is not scoped to a workspace, so this request must
# include the anthropic-workspace-id header ..." - verified against the live API on 2026-09-19
# with the key in .env. The SDK does not read the variable for the Messages API (it is only a
# Workload-Identity-Federation input), so when it is set nvplan forwards it as that one request
# header and otherwise sends nothing. Setting it with a workspace-scoped key is harmless.
WORKSPACE_ENV_VAR = "ANTHROPIC_WORKSPACE_ID"
WORKSPACE_HEADER = "anthropic-workspace-id"


def workspace_headers() -> dict[str, str]:
    """``{"anthropic-workspace-id": ...}`` when ``ANTHROPIC_WORKSPACE_ID`` is set, else ``{}``."""
    workspace = os.environ.get(WORKSPACE_ENV_VAR, "").strip()
    return {WORKSPACE_HEADER: workspace} if workspace else {}


def _ant_cli() -> str | None:
    """Path to the ``ant`` CLI, or None when it is not installed."""
    return shutil.which("ant")


def _ant_profile_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "anthropic"


def _ant_profile_available() -> bool:
    """True when ``ant auth login`` has stored a profile the SDK can pick up with no env var.

    Checked as files (cheap, no subprocess): the ``ant`` CLI plus at least one JSON file in its
    config directory."""
    if _ant_cli() is None:
        return False
    try:
        return any(_ant_profile_dir().glob("*.json"))
    except OSError:
        return False


def credentials_available() -> bool:
    """True when a real model can be built (an env credential, or a stored ``ant`` profile).

    Read lazily on every call, never cached at import, so ``nvplan.config``'s ``.env`` load and a
    test's ``monkeypatch.delenv`` are both visible."""
    if any(os.environ.get(name, "").strip() for name in CREDENTIAL_ENV_VARS):
        return True
    return _ant_profile_available()


def credential_hint() -> str:
    """One paragraph naming exactly what to do to get a credential (the ``MissingCredentials``
    message, and the API's 503 detail)."""
    options = [
        f"(1) put ANTHROPIC_API_KEY=sk-ant-... in {config.DOTENV_PATH} - it is git-ignored, "
        "nvplan.config loads it at import, and .env.example is the template",
        "(2) export ANTHROPIC_API_KEY=sk-ant-... in this shell",
    ]
    if _ant_cli() is not None:
        options.append("(3) run `ant auth login` - the stored profile is resolved without any environment variable")
    return (
        f"No Anthropic credential found, so the model {config.AI_MODEL!r} cannot be built. "
        + "Fix it one of these ways: "
        + "; ".join(options)
        + ". ANTHROPIC_WORKSPACE_ID is not a credential and does not help on its own: it is only "
        "forwarded as the anthropic-workspace-id request header, which an organization-level key "
        "needs and a workspace-scoped key does not. Verify with: uv run nvplan-ai-check. "
        "The deterministic planning engine does not depend on the AI layer."
    )


def _is_auth_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in ("api_key", "api key", "authentication", "credential", "unauthorized"))


# --------------------------------------------------------------------------- model

# What get_model sends, and - just as important - what it never sends (claude-opus-5 via
# langchain-anthropic 1.7.1):
# * temperature / top_p / top_k exist as ChatAnthropic fields but Opus 5 rejects every sampling
#   parameter with a 400. They are never set, at any level of the stack.
# * `thinking` is omitted entirely: Opus 5 runs adaptive thinking by default (the client fills in
#   {"type": "adaptive", "display": "summarized"} itself). `budget_tokens` was removed from the
#   API and returns a 400, so no thinking budget is ever passed either.
# * Depth is controlled by `effort` (the alias of ChatAnthropic.reasoning_effort), which the
#   client renders as request `output_config.effort` - the supported replacement for a budget.
# * `stream_usage` stays at its default True so every response carries usage_metadata, which
#   nvplan.ai.audit records per call (that is how prompt-cache effectiveness is measured).
# * The only header ever added is anthropic-workspace-id, and only when ANTHROPIC_WORKSPACE_ID is
#   set (see WORKSPACE_ENV_VAR: an organization-level key is rejected with a 400 without it).


def build_chat_model(
    model: str | None = None,
    *,
    max_tokens: int | None = None,
    effort: str | None = None,
    betas: Sequence[str] | None = None,
) -> BaseChatModel:
    """Construct the real ``ChatAnthropic`` (lazy import), defaults from ``config``.

    ``get_model`` is the entrypoint the touchpoints use; this one exists so the smoke check
    (``nvplan.ai.check``) can make one deliberately cheap call (small ``max_tokens``, effort
    ``low``) through exactly the same client construction."""
    if not credentials_available():
        raise MissingCredentials(credential_hint())
    from langchain_anthropic import ChatAnthropic  # lazy: offline tests need neither the package nor a key

    effort = effort or config.AI_EFFORT
    if effort not in config.AI_EFFORTS:
        raise ValueError(f"effort {effort!r} is not valid; expected one of {', '.join(config.AI_EFFORTS)}")
    kwargs: dict[str, Any] = {
        "model": model or config.AI_MODEL,
        "max_tokens": int(max_tokens or config.AI_MAX_TOKENS),
        "effort": effort,
    }
    flags = list(betas if betas is not None else config.AI_BETAS)
    if flags:
        kwargs["betas"] = flags
    headers = workspace_headers()  # only when the key is organization-level (see WORKSPACE_ENV_VAR)
    if headers:
        kwargs["default_headers"] = headers
    try:
        return ChatAnthropic(**kwargs)
    except Exception as exc:  # noqa: BLE001 - re-raised unless it is an auth problem
        if _is_auth_error(exc):
            raise MissingCredentials(credential_hint()) from exc
        raise


def get_model(model: str | BaseChatModel | None = None) -> BaseChatModel:
    """Return a chat model.

    A ``BaseChatModel`` is passed straight through untouched (that is how every offline test
    injects ``nvplan.ai.fake``). Anything else builds the configured ``ChatAnthropic``
    (``config.AI_MODEL`` / ``AI_MAX_TOKENS`` / ``AI_EFFORT`` / ``AI_BETAS``) and raises
    ``MissingCredentials`` with ``credential_hint()`` when there is no key."""
    if isinstance(model, BaseChatModel):
        return model
    return build_chat_model(model)


def model_version(model: BaseChatModel) -> str:
    """Identifier stored in ``ai_record.model_version`` (ChatAnthropic.model, or "fake")."""
    for attr in ("model_version", "model", "model_name"):
        v = getattr(model, attr, None)
        if isinstance(v, str) and v:
            return v
    return getattr(model, "_llm_type", type(model).__name__)


# --------------------------------------------------------------------------- prompt capture


class PromptCapture(BaseCallbackHandler):
    """Records the system prompt exactly as the first chat-model call received it."""

    def __init__(self) -> None:
        self.system_prompt: str | None = None

    def on_chat_model_start(self, serialized: Any, messages: list[list[BaseMessage]], **kwargs: Any) -> None:
        if self.system_prompt is not None:
            return
        for batch in messages:
            for m in batch:
                if m.type == "system":
                    self.system_prompt = _text(m.content)
                    return


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(p for p in parts if p)
    return str(content)


def _last_ai_text(messages: Sequence[BaseMessage]) -> str:
    for m in reversed(messages):
        if isinstance(m, AIMessage):
            t = _text(m.content).strip()
            if t:
                return t
    return ""


def _prompt_text(system_prompt: str, user_prompt: str) -> str:
    return f"---SYSTEM---\n{system_prompt}\n\n---USER---\n{user_prompt}"


def _response_text(messages: Sequence[BaseMessage], structured: Any) -> str:
    payload = structured.model_dump() if hasattr(structured, "model_dump") else structured
    return f"{_last_ai_text(messages)}\n\n---STRUCTURED---\n{json.dumps(payload, ensure_ascii=False, indent=2)}"


# --------------------------------------------------------------------------- agent builders


def _agent_middleware(policy: ContextPolicy | None, *, model: BaseChatModel, backend: Any, ctx: AiRunContext) -> list[Any]:
    """Context stack (read-only filesystem with eviction, summarization, caching) + the audit,
    in that order: the audit is last so it sees the request after summarization."""
    return [*build_context_middleware(policy, model=model, backend=backend), ContextAuditMiddleware(ctx)]


def touchpoint_tools(touchpoint: str, session_factory: SessionFactory, ctx: AiRunContext | None = None) -> list[BaseTool]:
    """The exact tool list a touchpoint agent gets."""
    if touchpoint not in _TOUCHPOINT_TOOLS:
        raise ValueError(f"unknown touchpoint {touchpoint!r}; expected one of {TOUCHPOINTS}")
    read_names, write_names = _TOUCHPOINT_TOOLS[touchpoint]
    tools = [t for t in make_read_tools(session_factory) if t.name in read_names]
    if write_names:
        tools += [t for t in make_write_tools(session_factory, ctx or AiRunContext()) if t.name in write_names]
    return tools


def build_touchpoint_agent(
    touchpoint: str,
    *,
    model: BaseChatModel,
    session_factory: SessionFactory,
    extra_context: AiRunContext | None = None,
    policy: ContextPolicy | None = None,
):
    """One deep agent for one touchpoint: its tools, its literal system prompt, its schema,
    its skill (``/skills/``), the context stack for ``policy`` (default: ``config``) and the
    per-call audit into ``extra_context.call_log``.

    ``extra_context`` is the run context shared with the write tools (default value,
    year, scenario, model version, and the ids the tools produce)."""
    from deepagents import create_deep_agent

    ctx = extra_context if extra_context is not None else AiRunContext()
    # A private backend, pruned to this touchpoint's own skill (nvplan.ai.context.make_backend) -
    # so its "Skills System" index carries only its own method, not the other nine.
    backend = make_backend(surface=touchpoint)
    return create_deep_agent(
        model=model,
        tools=touchpoint_tools(touchpoint, session_factory, ctx),
        system_prompt=prompts.TOUCHPOINT_SYSTEM_PROMPTS[touchpoint],
        response_format=ToolStrategy(TOUCHPOINT_SCHEMAS[touchpoint]),
        backend=backend,
        skills=[SKILLS_SOURCE],
        middleware=_agent_middleware(policy, model=model, backend=backend, ctx=ctx),
        name=f"nvplan-{touchpoint}",
    )


def touchpoint_subagents(
    session_factory: SessionFactory,
    ctx: AiRunContext | None = None,
    *,
    backend: Any,
    model: BaseChatModel,
    policy: ContextPolicy | None = None,
) -> list[dict[str, Any]]:
    """The three touchpoints as deepagents ``SubAgent`` dicts (same context stack and audit as
    the standalone agents; ``model`` is the subagents' model).

    Each subagent's own ``"skills"`` names its OWN scoped index route
    (``nvplan.ai.context.skills_index_route``), not the shared whole-directory ``SKILLS_SOURCE``:
    ``backend`` is the ONE object every subagent's ``SkillsMiddleware`` is built against
    (deepagents hands every declarative ``SubAgent`` the parent's own ``backend=``, never a
    per-subagent one - see ``make_shared_touchpoint_backend``'s docstring), so the per-surface
    scoping has to live in which route each spec names, not in the backend itself. ``backend``
    must therefore be one built by ``make_shared_touchpoint_backend(TOUCHPOINTS)`` (or a superset
    of it) for these routes to resolve."""
    ctx = ctx if ctx is not None else AiRunContext()
    subagents: list[dict[str, Any]] = []
    for tp in TOUCHPOINTS:
        spec: dict[str, Any] = {
            "name": _SUBAGENT_NAMES[tp],
            "description": _SUBAGENT_DESCRIPTIONS[tp],
            "system_prompt": prompts.TOUCHPOINT_SYSTEM_PROMPTS[tp],
            "tools": touchpoint_tools(tp, session_factory, ctx),
            "response_format": ToolStrategy(TOUCHPOINT_SCHEMAS[tp]),
            "skills": [skills_index_route(tp)],
            "middleware": _agent_middleware(policy, model=model, backend=backend, ctx=ctx),
            "model": model,
        }
        subagents.append(spec)
    return subagents


def build_advisor(
    model: BaseChatModel,
    session_factory: SessionFactory,
    ctx: AiRunContext | None = None,
    *,
    policy: ContextPolicy | None = None,
):
    """One orchestrating deep agent with the three touchpoints as subagents.

    The advisor itself owns no skill (``nvplan.ai.context.SKILL_SURFACE`` maps no skill to an
    "advisor" surface - it delegates all real work to its subagents, which each read their own),
    so it passes no ``skills=`` of its own; only ``touchpoint_subagents`` scopes one per
    subagent, via the shared backend built by ``make_shared_touchpoint_backend``."""
    from deepagents import create_deep_agent

    backend = make_shared_touchpoint_backend(TOUCHPOINTS)
    ctx = ctx or AiRunContext(model_version=model_version(model))
    return create_deep_agent(
        model=model,
        tools=make_read_tools(session_factory),
        system_prompt=prompts.ADVISOR_SYSTEM,
        subagents=touchpoint_subagents(session_factory, ctx, backend=backend, model=model, policy=policy),
        backend=backend,
        middleware=_agent_middleware(policy, model=model, backend=backend, ctx=ctx),
        name="nvplan-advisor",
    )


# --------------------------------------------------------------------------- context rendering


def _framework_text(positions_subset: Sequence[str] | None) -> tuple[str, int]:
    fw = load_env_framework()
    wanted = {p.strip() for p in positions_subset} if positions_subset else None
    lines: list[str] = []
    n = 0
    for dom in fw.get("domains", []):
        pos_lines = []
        for pos in dom.get("positions", []):
            if wanted is not None and pos["id"] not in wanted and dom["id"] not in wanted:
                continue
            affects = ", ".join(pos.get("affects", [])) or "-"
            pos_lines.append(f"  {pos['id']} {pos['name']} (affects {affects})")
            n += 1
        if pos_lines:
            lines.append(f"{dom['id']} {dom['name']}")
            lines.extend(pos_lines)
    return "\n".join(lines) if lines else "(no positions selected)", n


def _company_context(session) -> str:
    cats = {c.code: c for c in session.scalars(select(Category)).all()}
    by_code: dict[str, dict[int, float]] = {}
    for a in session.scalars(select(Actual)).all():
        code = next(c.code for c in cats.values() if c.id == a.category_id)
        by_code.setdefault(code, {})[a.year] = a.value
    rev = by_code.get("REV", {})
    if not rev:
        return "No actuals loaded yet."
    y0, y1 = min(rev), max(rev)
    cagr = (rev[y1] / rev[y0]) ** (1 / (y1 - y0)) - 1 if y1 > y0 and rev[y0] else 0.0
    costs = {c: v.get(y1, 0.0) for c, v in by_code.items() if c in ("MAT", "EXT", "PERS", "OTH")}
    total = sum(costs.values())
    pers_share = costs.get("PERS", 0.0) / total * 100 if total else 0.0
    return (
        f"Software company (ILLUSTRATIVE data). Revenue {rev[y0]:,.0f} k EUR in {y0} -> {rev[y1]:,.0f} k EUR in {y1} "
        f"(CAGR {cagr * 100:.1f}%). Costs {y1}: total {total:,.0f} k EUR, personnel share {pers_share:.0f}%; "
        f"material {costs.get('MAT', 0):,.0f}, external services {costs.get('EXT', 0):,.0f}, "
        f"other incl. depreciation {costs.get('OTH', 0):,.0f}."
    )


def _notes_text(session) -> str:
    cats = _categories(session)
    rows = session.scalars(select(ExternalNote).order_by(ExternalNote.id)).all()
    if not rows:
        return "(none)"
    return "\n".join(
        f"- note {n.id} [{cats[n.category_id].code if n.category_id else '-'} {n.year or '-'}, {n.source.value}, {n.author}]: {n.text}"
        for n in rows
    )


def _actuals_text(session, code: str) -> str:
    cat = session.scalar(select(Category).where(Category.code == code))
    rows = session.scalars(select(Actual).where(Actual.category_id == cat.id).order_by(Actual.year)).all() if cat else []
    if not rows:
        return "(none)"
    out = []
    prev = None
    for a in rows:
        growth = f" ({(a.value / prev - 1) * 100:+.1f}%)" if prev else ""
        out.append(f"- {a.year}: {a.value:,.1f}{growth}")
        prev = a.value
    return "\n".join(out)


def _rules_text() -> str:
    rules = load_control_table().get("revenue_proposal", {})
    return "\n".join(f"- {k}: {v}" for k, v in rules.items()) or "(none)"


def _table_text(data: dict[str, Any]) -> str:
    if "error" in data:
        return data["error"]
    lines = []
    for r in data["rows"]:
        line = (
            f"- {r['category_code']} ({r['name']}): plan {r['plan']:,.1f}, actual {r['actual']:,.1f}, "
            f"deviation {r['deviation']:+,.1f}"
        )
        if r.get("deviation_pct") is not None:
            line += f" ({r['deviation_pct']:+.1f}%)"
        if r.get("beta") is not None:
            line += f"; alpha {r['alpha']:,.1f}, beta {r['beta']:.4f}, R2 {r['r_squared']:.3f}"
        if "revenue_driven_part" in r:
            line += f"; revenue-driven {r['revenue_driven_part']:+,.1f}, residual {r['residual']:+,.1f}"
        lines.append(line)
    t = data.get("totals", {})
    if t.get("result_plan") is not None:
        lines.append(
            f"- Totals: revenue {t['revenue_plan']:,.1f} -> {t['revenue_actual']:,.1f}; costs {t['cost_plan']:,.1f} -> "
            f"{t['cost_actual']:,.1f}; result {t['result_plan']:,.1f} -> {t['result_actual']:,.1f}"
        )
    return "\n".join(lines) or "(no plan values / actuals for this scenario and year)"


# --------------------------------------------------------------------------- run helpers


REFUSAL_STOP_REASON = "refusal"


def _final_ai_message(messages: Sequence[BaseMessage]) -> AIMessage | None:
    for m in reversed(messages or []):
        if isinstance(m, AIMessage):
            return m
    return None


def refusal_details(message: AIMessage | None) -> dict[str, Any] | None:
    """``{"category": ..., "explanation": ...}`` when this message is a policy refusal, else None.

    Anthropic returns HTTP 200 with ``stop_reason="refusal"`` and a ``stop_details`` object
    (``{"type": "refusal", "category", "explanation"}``); ``stop_details`` is null for every
    other stop reason, so it is read defensively."""
    if message is None:
        return None
    meta = getattr(message, "response_metadata", None) or {}
    if not isinstance(meta, dict):
        return None
    stop = meta.get("stop_reason") or meta.get("finish_reason")
    if stop != REFUSAL_STOP_REASON:
        return None
    details = meta.get("stop_details")
    details = details if isinstance(details, dict) else {}
    return {"category": details.get("category"), "explanation": details.get("explanation")}


def _raise_if_refused(messages: Sequence[BaseMessage]) -> None:
    """Raise ``ModelRefused`` when the run's final AI message is a refusal (checked before the
    structured response, because a refused turn carries no structured output)."""
    details = refusal_details(_final_ai_message(messages))
    if details is None:
        return
    category = details.get("category") or "unspecified"
    explanation = details.get("explanation") or "(no explanation returned)"
    raise ModelRefused(
        f"the model declined this request (stop_reason={REFUSAL_STOP_REASON}, category={category}): "
        f"{explanation} - nothing was persisted"
    )


def _discard_partial_writes(session_factory: SessionFactory, ctx: AiRunContext) -> None:
    """Delete whatever the write tools committed mid-run (used on a refusal).

    ``record_revenue_proposal`` / ``record_external_note`` commit while the agent is running, so
    "persist nothing" has to be enforced after the fact."""
    if ctx.ai_record_id is None and not ctx.note_ids:
        return
    with session_factory() as s:
        for note in s.scalars(select(ExternalNote).where(ExternalNote.id.in_(ctx.note_ids or [-1]))).all():
            s.delete(note)
        if ctx.ai_record_id is not None:
            rec = s.get(AiRecord, ctx.ai_record_id)
            if rec is not None:
                s.delete(rec)
        s.commit()
    ctx.note_ids.clear()
    ctx.ai_record_id = None


def _invoke(agent, user_prompt: str, *, session_factory: SessionFactory, ctx: AiRunContext) -> tuple[dict[str, Any], PromptCapture]:
    """Run the agent, then refuse-check before anything is read out of the result."""
    capture = PromptCapture()
    result = agent.invoke({"messages": [{"role": "user", "content": user_prompt}]}, config={"callbacks": [capture]})
    try:
        _raise_if_refused(result.get("messages") or [])
    except ModelRefused:
        _discard_partial_writes(session_factory, ctx)
        raise
    if "structured_response" not in result or result["structured_response"] is None:
        raise RuntimeError("agent finished without a structured response")
    return result, capture


def _system_as_sent(capture: PromptCapture, authored: str) -> str:
    return capture.system_prompt if capture.system_prompt else authored


# --------------------------------------------------------------------------- entrypoints


def run_env_scan(
    session_factory: SessionFactory,
    *,
    model: str | BaseChatModel | None = None,
    positions_subset: list[str] | None = None,
    policy: ContextPolicy | None = None,
) -> AiRecord:
    """Touchpoint 1. Runs the scan, persists the ai_record and links the notes it wrote."""
    chat = get_model(model)
    ctx = AiRunContext(touchpoint=Touchpoint.env_scan, model_version=model_version(chat))
    framework_text, n_pos = _framework_text(positions_subset)
    with session_factory() as s:
        user_prompt = prompts.render_env_scan(
            company_context=_company_context(s),
            plan_from=config.PLAN_YEARS[0],
            plan_to=config.PLAN_YEARS[1],
            existing_notes=_notes_text(s),
            position_count=n_pos,
            framework_text=framework_text,
        )
    ctx.prompt_text = _prompt_text(prompts.ENV_SCAN_SYSTEM, user_prompt)

    agent = build_touchpoint_agent("env_scan", model=chat, session_factory=session_factory, extra_context=ctx, policy=policy)
    result, capture = _invoke(agent, user_prompt, session_factory=session_factory, ctx=ctx)
    scan: EnvScanResult = result["structured_response"]

    with session_factory() as s:
        rec = AiRecord(
            touchpoint=Touchpoint.env_scan,
            prompt_text=_prompt_text(_system_as_sent(capture, prompts.ENV_SCAN_SYSTEM), user_prompt),
            response_text=_response_text(result["messages"], scan),
            rationale=scan.summary,
            model_version=ctx.model_version,
            status=AiStatus.proposed,
            call_log_json=list(ctx.call_log),
        )
        s.add(rec)
        s.flush()
        if ctx.note_ids:
            for note in s.scalars(select(ExternalNote).where(ExternalNote.id.in_(ctx.note_ids))).all():
                note.ai_record_id = rec.id
        s.commit()
        ctx.ai_record_id = rec.id
        return rec


def run_revenue_proposal(
    session_factory: SessionFactory,
    *,
    scenario_kind: str,
    year: int,
    default_value: float,
    model: str | BaseChatModel | None = None,
    policy: ContextPolicy | None = None,
) -> AiRecord:
    """Touchpoint 2. ``default_value`` is the valorized default revenue for ``year`` (k EUR)."""
    chat = get_model(model)
    with session_factory() as s:
        scenario = _scenario_by_kind(s, scenario_kind)
        ctx = AiRunContext(
            touchpoint=Touchpoint.revenue_proposal,
            model_version=model_version(chat),
            scenario_id=scenario.id if scenario else None,
            year=int(year),
            default_value=float(default_value),
        )
        user_prompt = prompts.render_revenue_proposal(
            scenario_kind=scenario_kind,
            year=year,
            default_value=float(default_value),
            actuals_text=_actuals_text(s, "REV"),
            notes_text=_notes_text(s),
            rules_text=_rules_text(),
        )
    ctx.prompt_text = _prompt_text(prompts.REVENUE_PROPOSAL_SYSTEM, user_prompt)

    agent = build_touchpoint_agent("revenue_proposal", model=chat, session_factory=session_factory, extra_context=ctx, policy=policy)
    result, capture = _invoke(agent, user_prompt, session_factory=session_factory, ctx=ctx)
    proposal: RevenueProposal = result["structured_response"]

    with session_factory() as s:
        # Tool inserted the row during the run -> finalise it; otherwise validate + insert now.
        out = record_proposal(
            s,
            ctx,
            year=proposal.year,
            proposed_value=proposal.proposed_value,
            rationale=proposal.rationale,
            cited_note_ids=proposal.cited_note_ids,
            response_text=_response_text(result["messages"], proposal),
        )
        if isinstance(out, str):
            raise ProposalRejected(out)
        out.prompt_text = _prompt_text(_system_as_sent(capture, prompts.REVENUE_PROPOSAL_SYSTEM), user_prompt)
        out.call_log_json = list(ctx.call_log)
        s.commit()
        return out


def run_deviation_explanation(
    session_factory: SessionFactory,
    *,
    scenario_kind: str,
    year: int,
    model: str | BaseChatModel | None = None,
    policy: ContextPolicy | None = None,
) -> AiRecord:
    """Touchpoint 3. Persists the explanation as an ai_record (no proposed value).

    The structured result is cross-checked against the deterministic plan-vs-actual table
    first (``figures.check_explanation``); on any mismatch ``ExplanationRejected`` is raised
    and nothing is persisted."""
    chat = get_model(model)
    with session_factory() as s:
        scenario = _scenario_by_kind(s, scenario_kind)
        if scenario is None:
            raise ValueError(f"no scenario of kind {scenario_kind!r}")
        data = plan_vs_actual(s, scenario_kind, year)
        user_prompt = prompts.render_deviation_explanation(
            scenario_kind=scenario_kind, year=year, table_text=_table_text(data)
        )
        scenario_id = scenario.id
    ctx = AiRunContext(touchpoint=Touchpoint.deviation_explanation, model_version=model_version(chat), scenario_id=scenario_id, year=year)
    ctx.prompt_text = _prompt_text(prompts.DEVIATION_EXPLANATION_SYSTEM, user_prompt)

    agent = build_touchpoint_agent("deviation_explanation", model=chat, session_factory=session_factory, extra_context=ctx, policy=policy)
    result, capture = _invoke(agent, user_prompt, session_factory=session_factory, ctx=ctx)
    explanation: DeviationExplanation = result["structured_response"]

    # "Deviation calculated deterministically, explained by AI": the model's figures must be
    # the table's figures. Recomputed here (same code path as get_plan_vs_actual), and nothing
    # is written when the check fails.
    with session_factory() as s:
        check_explanation(explanation, plan_vs_actual(s, scenario_kind, year))

    with session_factory() as s:
        rec = AiRecord(
            touchpoint=Touchpoint.deviation_explanation,
            prompt_text=_prompt_text(_system_as_sent(capture, prompts.DEVIATION_EXPLANATION_SYSTEM), user_prompt),
            response_text=_response_text(result["messages"], explanation),
            rationale=explanation.summary,
            model_version=ctx.model_version,
            scenario_id=scenario_id,
            year=int(year),
            status=AiStatus.proposed,
            call_log_json=list(ctx.call_log),
        )
        s.add(rec)
        s.commit()
        ctx.ai_record_id = rec.id
        return rec
