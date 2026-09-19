"""The conversational assistant (UI.md Part 3): one read-mostly deep agent, structured output,
and the verification pass that makes it safe to demo.

The premise, and the one hard constraint (UI.md Part 3, quoted in full because it is the whole
point of this module):

    The assistant may not assert a figure it cannot source. Every number it produces is a
    citation that resolves to a row in the engine, or the answer is refused.

This is ``nvplan.ai.figures.check_explanation`` / ``mentions_figure`` generalized from one
touchpoint's numeric cross-check to an open-ended conversation: every ``figure`` segment's
``ref`` must resolve to a real row within tolerance (:func:`verify_answer`, part 1), and a
**backstop scan** over every ``text`` segment catches anything the model tried to slip into
prose instead of a citation (part 2). Verification runs entirely offline (no model call) and
*before* anything is persisted - exactly the discipline ``run_deviation_explanation`` already
applies to ``ExplanationRejected``.

Touchpoint reuse
-----------------
Adding an ``assistant`` member to ``nvplan.db.models.Touchpoint`` is a migration outside this
module's file ownership. This reuses ``Touchpoint.deviation_explanation`` for the assistant's
own record (see :data:`ASSISTANT_TOUCHPOINT`) - the closest existing member in spirit, since
that touchpoint's whole job is already "cite figures against a deterministic table or be
rejected" (``figures.check_explanation``), and unlike ``revenue_proposal`` it carries no
``proposed_value`` semantics of its own. The distinction is recorded in the record's
``rationale``, prefixed ``"[assistant] "`` (see :func:`ask`); a record's own touchpoint-specific
proposal (via ``record_revenue_proposal``, reused unmodified) still lands as its own
``ai_record`` with ``touchpoint=revenue_proposal``, exactly as the standalone touchpoint does.

Agent construction reuses ``nvplan.ai.agents``' private helpers (``_invoke``, ``PromptCapture``,
``_prompt_text``/``_system_as_sent``/``_response_text``, ``_discard_partial_writes``,
``_agent_middleware``, ``get_model``, ``model_version``) rather than duplicating them - same
context stack, same audit, same refusal handling as the three formal touchpoints.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal, Union

from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from nvplan import config
from nvplan.ai import agents, prompts
from nvplan.ai.context import ContextPolicy, make_backend
from nvplan.ai.tools import AiRunContext, SessionFactory, make_read_tools, make_write_tools
from nvplan.db.models import AiRecord, AiStatus, Parameter, PlanValue, Touchpoint
from nvplan.services.trace import trace_derivation

__all__ = [
    "AnswerRejected",
    "AssistantAnswer",
    "ClaimSegment",
    "FigureRef",
    "FigureSegment",
    "Proposal",
    "Segment",
    "TextSegment",
    "ask",
    "build_assistant_agent",
    "loose_figures",
    "verify_answer",
]

# The closest existing Touchpoint member for the assistant's own ai_record - see module doc.
ASSISTANT_TOUCHPOINT = Touchpoint.deviation_explanation
RATIONALE_PREFIX = "[assistant] "

ASSISTANT_READ_TOOLS: frozenset[str] = frozenset(
    {
        "get_plan_values",
        "get_plan_value",
        "get_trace",
        "get_parameters",
        "get_plan_vs_actual",
        "get_backtest_summary",
        "get_decisions",
        "get_decision",
        "get_claim_impact",
    }
)
ASSISTANT_WRITE_TOOLS: frozenset[str] = frozenset({"record_revenue_proposal"})


# --------------------------------------------------------------------------- answer shape (UI.md Part 3)


class TextSegment(BaseModel):
    type: Literal["text"] = "text"
    text: str = Field(description="Prose. No numbers except a year, a small count, or an identifier.")


class FigureRef(BaseModel):
    kind: Literal["plan_value", "parameter", "derivation", "claim"]
    id: int = Field(description="The id the tool returned for this row - not any other number.")


class FigureSegment(BaseModel):
    type: Literal["figure"] = "figure"
    label: str
    value: float
    unit: str
    ref: FigureRef


class ClaimSegment(BaseModel):
    type: Literal["claim"] = "claim"
    claim_id: int
    title: str
    status: str


Segment = Annotated[Union[TextSegment, FigureSegment, ClaimSegment], Field(discriminator="type")]


class Proposal(BaseModel):
    """The card the Ask view renders with confirm/reject - the same revenue proposal the
    existing gate confirms or rejects (UI.md Part 3: "Confirming runs the existing gate and
    cascade untouched"). ``ai_record_id`` is filled in by :func:`ask` from what
    ``record_revenue_proposal`` actually wrote, never taken from the model's own claim."""

    ai_record_id: int | None = None
    year: int
    proposed_value: float
    rationale: str
    category_code: str = "REV"


class AssistantAnswer(BaseModel):
    """``ask()``'s return value; also the deep agent's structured output schema (``ai_record_id``
    and ``usage`` are filled in afterwards, once the answer has verified and persisted, so they
    carry defaults the model is never required to produce)."""

    segments: list[Segment] = Field(default_factory=list)
    proposal: Proposal | None = None
    ai_record_id: int = Field(default=-1, description="set by ask() after the answer verifies and persists")
    usage: dict[str, int] = Field(default_factory=dict, description="real token usage; set by ask()")


class AnswerRejected(ValueError):
    """The assistant's answer cited a figure it could not source, or left a loose number in
    prose; nothing was persisted (same discipline as ``ExplanationRejected``)."""


# --------------------------------------------------------------------------- part 1: figure / claim refs


def _resolve_figure_ref(session: Session, kind: str, ref_id: int) -> tuple[bool, float | None, list[float] | None]:
    """``(found, value, candidates)``. ``candidates`` is set only for "parameter" (which has
    several numeric fields and no single "value" column); the figure passes when it matches
    ANY of them within tolerance."""
    if kind == "plan_value":
        row = session.get(PlanValue, ref_id)
        return (row is not None, row.value if row is not None else None, None)
    if kind == "parameter":
        row = session.get(Parameter, ref_id)
        if row is None:
            return False, None, None
        return True, None, [row.alpha, row.beta, row.r_squared, row.valorization_rate]
    if kind == "derivation":
        try:
            node = trace_derivation(session, ref_id, max_depth=0)
        except LookupError:
            return False, None, None
        # A "parameter" node has no single .value (see nvplan.services.trace._node_for) - cite
        # it as ref kind "parameter" with the Parameter row's own id instead.
        return True, node.value, None
    if kind == "claim":
        try:
            from provenance import Claim
        except Exception:  # noqa: BLE001
            return False, None, None
        try:
            row = session.get(Claim, ref_id)
        except Exception:  # noqa: BLE001 - finance-only database, no claim table
            try:
                session.rollback()
            except Exception:  # noqa: BLE001
                pass
            return False, None, None
        if row is None or not row.effect_json or row.effect_json.get("value") is None:
            return False, None, None
        return True, float(row.effect_json["value"]), None
    return False, None, None


def _check_figure(session: Session, index: int, seg: FigureSegment, abs_tol: float) -> list[str]:
    found, value, candidates = _resolve_figure_ref(session, seg.ref.kind, seg.ref.id)
    where = f"segment {index} (figure {seg.label!r}, ref {seg.ref.kind}:{seg.ref.id})"
    if not found:
        return [f"{where}: does not resolve to a real row"]
    if candidates is not None:
        if not any(c is not None and abs(seg.value - c) <= abs_tol for c in candidates):
            return [f"{where}: value {seg.value} matches none of the parameter's fields {candidates} within {abs_tol}"]
        return []
    if value is None or abs(seg.value - value) > abs_tol:
        return [f"{where}: value {seg.value} vs stored {value}"]
    return []


def _check_claim_segment(session: Session, index: int, seg: ClaimSegment) -> list[str]:
    try:
        from provenance import Claim
    except Exception as e:  # noqa: BLE001
        return [f"segment {index} (claim {seg.claim_id}): brain tables not available: {e}"]
    try:
        row = session.get(Claim, seg.claim_id)
    except Exception as e:  # noqa: BLE001 - finance-only database, no claim table
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return [f"segment {index} (claim {seg.claim_id}): brain tables not available: {e}"]
    if row is None:
        return [f"segment {index}: claim {seg.claim_id} does not exist"]
    return []


# --------------------------------------------------------------------------- part 2: the backstop scan

# A four-digit year is allowed only inside the historical regression window or the plan
# horizon (nvplan.config) - never by virtue of merely looking like a year.
_YEAR_ALLOWED: frozenset[int] = frozenset(
    range(config.REGRESSION_WINDOW[0], config.REGRESSION_WINDOW[1] + 1)
) | frozenset(range(config.PLAN_YEARS[0], config.PLAN_YEARS[1] + 1))

# A count small enough to be an ordinal used in prose ("3 scenarios"), never a figure.
_SMALL_COUNT_MAX = 20

# Runs of identifier-ish characters: a run that mixes letters and digits (a decision slug like
# "2026-09-20-sunset-legacy-import", or a code like "param:PERS" once it contains a digit) is
# never a number, however many digits it carries, and is masked out before the numeric scan.
_IDENTIFIER_RUN_RE = re.compile(r"[A-Za-z0-9_./:\-]+")

# Any number-like token left after masking: an optional sign, a digit run (optionally grouped by
# spaces/commas/nbsp), an optional decimal part, an optional percent sign. Deliberately permissive
# (over-matching a malformed number into two flagged pieces is safe; under-matching a real figure
# is not) - see the module tests for the cases this must get right.
_NUMERIC_RE = re.compile(
    r"(?<![\w.])[+\-−–]?\d+(?:[ ,  ]\d{3})*(?:\.\d+)?%?(?![\w])"
)


def _mask_identifiers(text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        s = m.group(0)
        if any(c.isalpha() for c in s) and any(c.isdigit() for c in s):
            return " " * len(s)  # a word/identifier, e.g. "param:PERS", a decision slug - never a number
        return s

    return _IDENTIFIER_RUN_RE.sub(repl, text)


def _is_allowed_year(token: str) -> bool:
    return token.isdigit() and len(token) == 4 and int(token) in _YEAR_ALLOWED


def _is_allowed_count(masked: str, match: re.Match[str]) -> bool:
    token = match.group(0)
    if not token.isdigit() or len(token) > 2:  # a plain, small, unsigned integer only
        return False
    if int(token) > _SMALL_COUNT_MAX:
        return False
    tail = masked[match.end() :]
    return re.match(r"\s+[A-Za-z]", tail) is not None  # "3 scenarios", not a bare "3"


def loose_figures(text: str) -> list[str]:
    """Number-like tokens in ``text`` that are not a year in window/horizon, a small count used
    as an ordinal, or part of a word/identifier (UI.md Part 3's backstop scan). Empty when
    ``text`` carries nothing to reject. A spelled-out number ("three scenarios") is never
    flagged - it contains no digit at all."""
    if not text:
        return []
    masked = _mask_identifiers(text)
    offenders: list[str] = []
    for m in _NUMERIC_RE.finditer(masked):
        token = m.group(0)
        if _is_allowed_year(token) or _is_allowed_count(masked, m):
            continue
        offenders.append(token)
    return offenders


def _check_text(index: int, seg: TextSegment) -> list[str]:
    offenders = loose_figures(seg.text)
    if not offenders:
        return []
    return [
        f"segment {index} (text): loose, uncited figure(s) {offenders!r} in prose "
        f"{seg.text[:160]!r} - state figures only as a `figure` segment"
    ]


# --------------------------------------------------------------------------- verification entrypoint


def verify_answer(answer: AssistantAnswer, session: Session, *, abs_tol: float = 0.05) -> None:
    """UI.md Part 3's verification, run before anything is returned or persisted.

    1. Every ``figure`` segment's ``ref`` resolves to a real row and its ``value`` matches that
       row within ``abs_tol``.
    2. Every ``claim`` segment's ``claim_id`` exists.
    3. The backstop scan: no ``text`` segment carries a loose (uncited) number.

    Raises :class:`AnswerRejected` listing every failure; returns ``None`` when the answer is
    clean."""
    problems: list[str] = []
    for i, seg in enumerate(answer.segments):
        if isinstance(seg, FigureSegment):
            problems.extend(_check_figure(session, i, seg, abs_tol))
        elif isinstance(seg, ClaimSegment):
            problems.extend(_check_claim_segment(session, i, seg))
        elif isinstance(seg, TextSegment):
            problems.extend(_check_text(i, seg))
    if problems:
        raise AnswerRejected(
            f"assistant answer rejected ({len(problems)} unsourced figure(s)): " + "; ".join(problems)
        )


# --------------------------------------------------------------------------- agent construction


def assistant_tools(session_factory: SessionFactory, ctx: AiRunContext) -> list[Any]:
    """The assistant's exact tool list: the nine read tools UI.md Part 3 asks for, plus the one
    existing write path (the revenue-proposal gate, reused unmodified)."""
    tools = [t for t in make_read_tools(session_factory) if t.name in ASSISTANT_READ_TOOLS]
    tools += [t for t in make_write_tools(session_factory, ctx) if t.name in ASSISTANT_WRITE_TOOLS]
    return tools


def build_assistant_agent(
    *,
    model: BaseChatModel,
    session_factory: SessionFactory,
    extra_context: AiRunContext | None = None,
    policy: ContextPolicy | None = None,
):
    """One deep agent for the conversational view: read tools + the proposal gate, the literal
    system prompt, ``AssistantAnswer`` as structured output, and the same context/audit stack
    the three formal touchpoints get (``nvplan.ai.agents._agent_middleware``)."""
    from deepagents import create_deep_agent

    ctx = extra_context if extra_context is not None else AiRunContext()
    backend = make_backend()
    return create_deep_agent(
        model=model,
        tools=assistant_tools(session_factory, ctx),
        system_prompt=prompts.ASSISTANT_ASK_SYSTEM,
        response_format=ToolStrategy(AssistantAnswer),
        backend=backend,
        middleware=agents._agent_middleware(policy, model=model, backend=backend, ctx=ctx),
        name="nvplan-assistant",
    )


# --------------------------------------------------------------------------- entrypoint


def ask(
    session_factory: SessionFactory,
    question: str,
    *,
    scenario_kind: str = "base",
    model: str | BaseChatModel | None = None,
    policy: ContextPolicy | None = None,
) -> AssistantAnswer:
    """``POST /assistant/ask``'s implementation. Builds the agent, invokes it, verifies the
    structured answer against the database *before* persisting anything, then persists one
    ``ai_record`` (touchpoint reused, see module doc) with the literal prompt/response and the
    per-call audit log. On rejection, discards whatever the proposal tool wrote mid-run (the
    same discipline ``run_deviation_explanation`` applies to ``ExplanationRejected``) and
    re-raises; no ``ai_record`` exists for a rejected answer."""
    chat = agents.get_model(model)
    ctx = AiRunContext(touchpoint=ASSISTANT_TOUCHPOINT, model_version=agents.model_version(chat))
    user_prompt = prompts.render_assistant_ask(question=question, scenario_kind=scenario_kind)
    ctx.prompt_text = agents._prompt_text(prompts.ASSISTANT_ASK_SYSTEM, user_prompt)

    agent = build_assistant_agent(model=chat, session_factory=session_factory, extra_context=ctx, policy=policy)
    result, capture = agents._invoke(agent, user_prompt, session_factory=session_factory, ctx=ctx)
    draft: AssistantAnswer = result["structured_response"]

    proposal_record_id = ctx.ai_record_id  # set by record_revenue_proposal, if the model called it
    try:
        if draft.proposal is not None and proposal_record_id is None:
            # The model asserted a proposal card without recording it through the write tool -
            # the only sanctioned way to create one (UI.md Part 3: "Nothing may write except
            # the existing proposal path"). Treat it exactly like any other unsourced figure.
            raise AnswerRejected(
                "assistant answer rejected (1 unsourced proposal): a `proposal` was returned "
                "without recording it via record_revenue_proposal; nothing was persisted"
            )
        with session_factory() as s:
            verify_answer(draft, s)
    except AnswerRejected:
        agents._discard_partial_writes(session_factory, ctx)
        raise

    if draft.proposal is not None and proposal_record_id is not None:
        draft.proposal.ai_record_id = proposal_record_id

    with session_factory() as s:
        rec = AiRecord(
            touchpoint=ASSISTANT_TOUCHPOINT,
            prompt_text=agents._prompt_text(agents._system_as_sent(capture, prompts.ASSISTANT_ASK_SYSTEM), user_prompt),
            response_text=agents._response_text(result["messages"], draft),
            rationale=RATIONALE_PREFIX + question.strip()[:500],
            model_version=ctx.model_version,
            status=AiStatus.proposed,
            call_log_json=list(ctx.call_log),
        )
        s.add(rec)
        s.commit()
        ctx.ai_record_id = rec.id
        draft.ai_record_id = rec.id

    draft.usage = dict(ctx.total_usage)
    return draft
