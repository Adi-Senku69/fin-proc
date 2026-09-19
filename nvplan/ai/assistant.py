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

Touchpoint
----------
``nvplan.db.models.Touchpoint`` carries its own ``assistant`` member for exactly this module's
own record (see :data:`ASSISTANT_TOUCHPOINT`); it is additive (the column is ``Enum(Touchpoint)``,
stored by member *name*, so existing rows under the three formal touchpoints are unaffected -
see ``tests/test_assistant.py``). The record's ``rationale`` is the question itself, verbatim, no
longer a ``"[assistant] "``-prefixed reuse of ``deviation_explanation``'s rationale - a prefix in
free text was not a real category and made the two indistinguishable to any query. A record's own
touchpoint-specific proposal (via ``record_revenue_proposal``, reused unmodified) still lands as
its own ``ai_record`` with ``touchpoint=revenue_proposal``, exactly as the standalone touchpoint
does.

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

# The assistant's own ai_record touchpoint - see module doc.
ASSISTANT_TOUCHPOINT = Touchpoint.assistant

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
    text: str = Field(
        description="Prose. No digits except a year in window/horizon or an identifier - write any count as a word."
    )


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

# Runs of identifier-ish characters that MIGHT be one of the explicit shapes below - never a
# generic "mixes a letter and a digit" heuristic. That heuristic used to mask ANY such run,
# which is how scientific notation ("1e5") and a fused unit ("22900kEUR") escaped: both mix a
# letter and a digit but are not identifiers at all. Each run found here is masked only when it
# matches one of the named shapes; everything else - including those two - falls through to the
# numeric scan below.
_IDENTIFIER_RUN_RE = re.compile(r"[A-Za-z0-9_./:\-]+")

# Shape 1: a token with a colon - a parameter code ("param:PERS") or a ledger key
# ("plan:base:REV:2027", "actual:REV:2025", "depr:2028" - see nvplan.core.ledger). Every ledger
# key's numeric segment is either a 4-digit year or a short 1-2 digit index, never an arbitrary
# figure, so a colon-separated segment that is ALL digits and isn't one of those lengths breaks
# the shape - closing an evasion this rule would otherwise open ("revenue:22900"). A digit
# segment that merely happens to be 4 digits (see the module doc) is a smaller residual.
_COLON_SHAPE = re.compile(r":")


def _is_colon_shape(run: str) -> bool:
    if not _COLON_SHAPE.search(run):
        return False
    return all(not p.isdigit() or len(p) in (1, 2, 4) for p in run.split(":"))


# Shape 2: an ISO date (a decision's own `date`, not a figure). Month/day are range-checked so a
# fabricated, syntactically-ISO-shaped token can't be waved through on shape alone - though the
# year slot itself is not range-checked (a decision may predate the plan horizon); see the
# module doc for the residual this leaves open.
_ISO_DATE_SHAPE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _is_iso_date_shape(run: str) -> bool:
    m = _ISO_DATE_SHAPE.match(run)
    if not m:
        return False
    month, day = int(m.group(2)), int(m.group(3))
    return 1 <= month <= 12 and 1 <= day <= 31


# Shape 3: a slug of 3+ hyphen-separated parts, at least one of them alphabetic - a decision
# slug ("2026-09-20-sunset-legacy-import"), never a number even though several parts are digits.
# Any ALL-digit part must be 2 or 4 digits (a date component), exactly as a real decision slug's
# date prefix is - not an arbitrary length, which is how a fabricated 3-part slug could otherwise
# smuggle a real figure past this rule ("22900-legacy-import"; see the module doc).
def _is_slug_shape(run: str) -> bool:
    parts = run.split("-")
    if len(parts) < 3 or not all(parts):
        return False
    if not any(p.isalpha() for p in parts):
        return False
    return all(not p.isdigit() or len(p) in (2, 4) for p in parts)


# Shape 4: a label that reads alphabetic-then-ordinal-digit, one or more dot-joined segments
# ("Q1", "D2.P4" from the scan framework). Each segment's digit run is capped at two digits -
# the framework's own labels never need more (D1-D6, P1-P9, Q1-Q4) - specifically so a fused
# figure dressed up as a label ("q1500") can't hide behind an unbounded digit run; see the
# module doc for the narrower residual this still leaves ("q15").
_LABEL_SHAPE = re.compile(r"^[A-Za-z]{1,4}\d{1,2}(?:\.[A-Za-z]{1,4}\d{1,2})*$")

# Shape 5: a number hyphen-fused to a single word as a compound modifier ("54-position", as in
# "the 54-position framework") - grammatically a name for a fixed thing, not an assertion of a
# quantity. Contrast a fused *unit* with no hyphen ("22900kEUR"), which IS a figure (see
# _NUMERIC_RE below) - the hyphen, not the size of the number, is what marks this as a label. The
# digit run is capped at 3 digits (comfortably above "54", the spec's own example) so this can't
# become an unbounded smuggling route on its own; a number under 1000 fused this way
# ("500-widgets") is a smaller, documented residual (see the module doc) rather than an open one.
_HYPHEN_LABEL_SHAPE = re.compile(r"^\d{1,3}-[A-Za-z]+$")


def _is_identifier_shape(run: str) -> bool:
    if _is_colon_shape(run):
        return True
    if _is_iso_date_shape(run):
        return True
    if _HYPHEN_LABEL_SHAPE.match(run):
        return True
    if _LABEL_SHAPE.match(run):
        return True
    return _is_slug_shape(run)


# Any number-like token left after masking: an optional sign; a digit run (optionally grouped by
# spaces/commas/nbsp) with an optional decimal part, OR a decimal with no leading integer part
# ("`.488`"); an optional scientific-notation exponent ("`1e5`", "`1E-5`"); an optional percent
# sign; and - unlike the old trailing boundary, which refused to match right up against a
# following letter and thereby hid a fused unit - an optional run of fused letters, so a figure
# glued to a unit ("`22900kEUR`") is caught as one offending token instead of silently skipped.
# Deliberately permissive (over-matching a malformed number into two flagged pieces is safe;
# under-matching a real figure is not) - see the module tests for the cases this must get right.
# The fused-*prefix* direction ("kEUR22900") is this regex's own leading boundary refusing to
# start a match right after a letter - left as-is here and covered separately by
# ``_leading_fusions`` below, not by widening this pattern.
_NUMERIC_RE = re.compile(
    r"(?<![\w.])"
    r"[+\-−–]?"
    r"(?:\d+(?:[ ,\u00a0]\d{3})*(?:\.\d+)?|\.\d+)"
    r"(?:[eE][+\-]?\d+)?"
    r"%?"
    r"[A-Za-z]*"
    r"(?![\w])"
)


def _mask_identifiers(text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        s = m.group(0)
        return " " * len(s) if _is_identifier_shape(s) else s

    return _IDENTIFIER_RUN_RE.sub(repl, text)


# Hole (UI.md Part 3 backstop scan): a figure fused to a UNIT WRITTEN AFTER it ("22900kEUR") was
# already caught by _NUMERIC_RE's trailing letter run; one fused to a unit written BEFORE it
# ("USD22900") was not, because _NUMERIC_RE's leading boundary deliberately refuses to start a
# match right after a letter (see its own comment) - that boundary stays as-is for _NUMERIC_RE,
# but a letter run immediately followed by digits is now checked separately. Exempted only when
# the digit run is short enough to be a real framework label ("Q1", "D2" - at most two digits,
# the same cap _LABEL_SHAPE already applies) or is itself a four-digit year already inside the
# historical window or plan horizon ("FY2027"). Runs over the ALREADY-MASKED text so a genuine
# masked shape (a ledger key, a decision slug) that happens to embed letters and digits is never
# double-flagged.
_LEADING_FUSION_RE = re.compile(r"[A-Za-z]+(\d+)")


def _leading_fusions(masked: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for m in _LEADING_FUSION_RE.finditer(masked):
        digits = m.group(1)
        if len(digits) <= 2:
            continue
        if len(digits) == 4 and int(digits) in _YEAR_ALLOWED:
            continue
        found.append((m.start(), m.group(0)))
    return found


def _is_allowed_year(token: str) -> bool:
    return token.isdigit() and len(token) == 4 and int(token) in _YEAR_ALLOWED


def loose_figures(text: str) -> list[str]:
    """The rule, closed: prose may contain no digits except a year inside the historical window
    or plan horizon, and identifiers of the recognised shapes (see ``_is_identifier_shape``).
    Everything else is a loose figure.

    There is deliberately no exemption for a count written in digits ("3 scenarios"): a
    deny-list of the words that follow a bare number ("bp", "GBP", "mn", "grand", ...) can never
    be complete, so the count exemption that used to key off that list has been removed rather
    than patched again. A spelled-out count ("three scenarios") is unaffected - it contains no
    digit at all and was never in scope of this scan.

    Number-like tokens in ``text`` that are not a year in window/horizon or part of a
    word/identifier (UI.md Part 3's backstop scan). Empty when ``text`` carries nothing to
    reject.

    Two independent scans over the same masked text, merged back into text order: the ordinary
    numeric scan (``_NUMERIC_RE``, trailing fusion included) and the leading-fusion scan
    (``_leading_fusions``, a unit written before the figure with no space - see its own
    comment). Merging by position rather than concatenating keeps the offender list in reading
    order regardless of which scan found which token, and the two scans never overlap (one only
    ever matches immediately after a letter, the other's leading boundary refuses to)."""
    if not text:
        return []
    masked = _mask_identifiers(text)
    found: list[tuple[int, str]] = list(_leading_fusions(masked))
    for m in _NUMERIC_RE.finditer(masked):
        token = m.group(0)
        if _is_allowed_year(token):
            continue
        found.append((m.start(), token))
    found.sort(key=lambda pair: pair[0])
    return [token for _, token in found]


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
            rationale=question.strip()[:500],
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
