"""bridge/sweep.py — the sweep (PLATFORM.md §12.5, §12.6, work package B4).

Everything built through B1-B3 flows *into* the brain: a hand-written file, an ingested scan
finding, a drafted decision or hypothesis. Nothing yet looked back over what is already there and
said what has gone stale. PLATFORM.md §12.5 names four things worth noticing without being asked:
a decided decision whose reversal condition may have tripped, an ``open`` hypothesis well past
its test, an evidence link whose target vanished, a decision with no evidence fresher than itself.
This module is the first three, plus two checks (§12.6's own framing: "orphaned links... and
stale evidence") that need a live derivation lookup or the claim-link graph rather than pure text,
so they cannot live in ``brainkit.validate`` (which never opens a session).

The central guardrail (PLATFORM.md §12.5: "it proposes; it never promotes")
-----------------------------------------------------------------------------
Every function below is a SELECT, or a pure read of files already on disk. Nothing here calls
``session.add``/``session.execute`` with an INSERT/UPDATE/DELETE, and nothing calls
``Path.write_text``. A sweep that could flip a ``decided`` record back to ``pending`` because a
reversal condition looks tripped would be the AI reversing a human decision - exactly the failure
mode §12.1's whole design exists to rule out ("the model proposes, a named human confirms"). The
model is not even trusted to decide *this*: :func:`evaluate_reversal_conditions` calls the model
only for a condition neither mechanical template below can parse, and that call's answer is
carried in :attr:`ReversalVerdict.detail` while :attr:`ReversalVerdict.tripped` stays ``None`` -
an advisory verdict is never able to say "tripped" or "not tripped", only a mechanically evaluated
one can. See ``tests/test_bridge_sweep.py``'s non-mutation test for the enforced version of this
claim: every brain/ file's bytes and every claim's status are the same before and after a sweep.

Reversal conditions are prose (PLATFORM.md §4.4 asks for "an observable condition", not a
machine-readable one), so most of them are not mechanically evaluable at all. This module draws
the line the task that produced it asked for: a condition is checked mechanically only when its
own wording names a checkable quantity the engine actually holds -

  * "R-squared for the <CODE> regression drops below <threshold>" - checked against the latest
    fitted :class:`~nvplan.db.models.Parameter` for that category (the same figure the
    ``personnel-cost-assumption`` decision's own reversal condition already names in prose).
  * "planned <CODE> for <year> (falls/drops below | exceeds/rises above) <value>" - checked
    against the current base-scenario :class:`~nvplan.db.models.PlanValue` for that category/year.

Neither template understands every decision's prose (most conditions in ``brain/`` today read
like "if things change materially" dressed up in specifics no engine holds - a headcount count, a
support-ticket count, a customer-migration count) - those fall through to the advisory pass,
clearly labelled, never silently dropped.

Deterministic checks needing no model at all (requirement 1 of the work package)
-------------------------------------------------------------------------------
Two of the four are already computed by ``brainkit.validate`` (PLATFORM.md §9) and are reused
here verbatim rather than reinvented - see :data:`REUSED_VALIDATE_CODES`:

  * ``unresolved_link`` - a path-typed evidence tag (``ingestion``/``source``) whose target no
    longer resolves on disk. This is also "evidence whose target_path no longer exists": the same
    check, the same code, not a second implementation of the same fact.
  * ``effect_not_wired`` - a decided decision's quantified effect names a category the bridge
    does not drive (PLATFORM.md §7.1; only ``REV`` is wired in P2).

Two are new, because ``brainkit.validate`` has no session and cannot see either fact:

  * :data:`COMPUTED_DERIVATION_MISSING` - a ``(computed, <key>)`` evidence tag that resolved at
    some earlier reindex now resolves to nothing (:func:`bridge.lookup.make_derivation_lookup`
    re-run live, not the ``resolved`` column's stale snapshot).
  * :data:`BROKEN_SUPERSESSION_CHAIN` - a decision claiming ``status: superseded`` that is the
    target of no other decision's ``supersedes`` link - the chain a human expects when they see
    that status is missing its other half.

Not implemented: "hypotheses still open well past the decision they were meant to test" (no
``tests``-relation ``ClaimLink`` is populated by ``brainkit.indexer`` today - there is nothing
structural to check yet) and the freshness half of "no evidence from any source newer than the
decision itself" (ingestion claims carry no indexed ``date`` - see ``brainkit.parse.
_parse_generic_file`` - so answering it would mean re-parsing every cited file's own "## Date"
section here, a second parser for a fact ``brainkit.parse`` could just as well index). Both are
left for a future pass; see this module's own docstring in the work package's final report.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import select
from sqlalchemy.orm import Session

from nvplan import config
from nvplan.ai.agents import resolve_model
from nvplan.ai.fake import SWEEP_ADVISORY_MARKER
from nvplan.ai.figures import _categories, _latest_parameters
from nvplan.services.trace import find_plan_value

from provenance import Claim, ClaimKind, ClaimLink, Evidence, LinkRelation, TagKind, parse_row

from brainkit.validate import Finding, validate_tree

from bridge.lookup import make_derivation_lookup

__all__ = [
    "COMPUTED_DERIVATION_MISSING",
    "BROKEN_SUPERSESSION_CHAIN",
    "REUSED_VALIDATE_CODES",
    "ReversalVerdict",
    "SweepReport",
    "evaluate_reversal_conditions",
    "run_sweep",
    "render_sweep_report",
]

# Codes reused verbatim from brainkit.validate (PLATFORM.md §9) - see the module docstring.
REUSED_VALIDATE_CODES: tuple[str, ...] = ("unresolved_link", "effect_not_wired")

# New codes: neither is producible by brainkit.validate, which never opens a session.
COMPUTED_DERIVATION_MISSING = "computed_derivation_missing"
BROKEN_SUPERSESSION_CHAIN = "broken_supersession_chain"


# --------------------------------------------------------------------------- reversal conditions


@dataclass(frozen=True)
class ReversalVerdict:
    """One decided decision's reversal condition, checked once.

    ``tripped`` is ``True``/``False`` only when ``mechanism`` is ``"r_squared"`` or
    ``"plan_value"`` - a real comparison against a real figure the engine holds right now.
    ``tripped`` is always ``None`` when ``mechanism == "advisory"``: the condition's own prose
    named nothing this module knows how to check mechanically, so a model may have looked at it
    (``detail`` carries whatever it said), but nothing - not this module, not the model - decided
    whether it has tripped. That is left to the human PLATFORM.md §12.1 requires."""

    claim_id: int
    decision_slug: str
    decision_title: str
    condition_text: str
    mechanism: str  # "r_squared" | "plan_value" | "advisory"
    tripped: bool | None
    detail: str


_R2_RE = re.compile(
    r"r-squared\s+for\s+the\s+(?P<code>[a-z]+)\s+regression\s+drops?\s+below\s+(?P<threshold>[\d.]+)",
    re.IGNORECASE,
)

_PLAN_VALUE_RE = re.compile(
    r"planned\s+(?P<code>[a-z]+)\s+for\s+(?P<year>\d{4})\s+"
    r"(?P<direction>falls?\s+below|drops?\s+below|is\s+below|exceeds|rises?\s+above|is\s+above)\s+"
    r"(?P<value>[\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)


def _check_r_squared(session: Session, code: str, threshold: float) -> tuple[bool, str] | None:
    """``None`` when the named category isn't one the plan actually holds a parameter for
    (nothing to check, so this template does not match after all - falls through to advisory
    rather than reporting a false negative); otherwise the real (tripped, detail) verdict."""
    code = code.strip().upper()
    if code not in config.CATEGORY_CODES:
        return None
    cat_id = next((cid for cid, cat in _categories(session).items() if cat.code == code), None)
    if cat_id is None:
        return None
    param = _latest_parameters(session).get(cat_id)
    if param is None or param.r_squared is None:
        return None
    tripped = param.r_squared < threshold
    detail = (
        f"{code} regression R-squared is {param.r_squared:.4f}; condition trips when it drops "
        f"below {threshold:.4f}"
    )
    return tripped, detail


def _check_plan_value(session: Session, code: str, year: int, direction: str, raw_value: str) -> tuple[bool, str] | None:
    code = code.strip().upper()
    if code not in config.CATEGORY_CODES:
        return None
    if not (config.PLAN_YEARS[0] <= year <= config.PLAN_YEARS[1]):
        return None
    try:
        threshold = float(raw_value.replace(",", ""))
    except ValueError:
        return None
    try:
        pv = find_plan_value(session, scenario_kind="base", category_code=code, year=year)
    except LookupError:
        return None
    direction_key = direction.strip().lower()
    if "below" in direction_key:
        tripped, comparator = pv.value < threshold, "below"
    elif "above" in direction_key or "exceeds" in direction_key:
        tripped, comparator = pv.value > threshold, "above"
    else:  # pragma: no cover - unreachable, the regex only matches the alternatives above
        return None
    detail = (
        f"planned {code} {year} = {pv.value:,.1f} kEUR; condition trips when it is {comparator} "
        f"{threshold:,.1f} kEUR"
    )
    return tripped, detail


def _advisory_pass(model: BaseChatModel, claim: Claim, condition_text: str) -> str:
    """One direct, tool-free model call - never an agent loop, no write path anywhere near it -
    that reads a reversal condition's prose and returns a short note for a human to weigh. The
    prompt carries :data:`nvplan.ai.fake.SWEEP_ADVISORY_MARKER` so
    ``nvplan.ai.fake.DeterministicChatModel`` (the offline default - see
    ``nvplan.ai.agents.resolve_model``'s own docstring) can answer this reactively, from the real
    condition text, with no network - the same discipline every other AI-layer entrypoint follows.
    This function returns only the model's words; it never turns them into a verdict - see
    :class:`ReversalVerdict`'s own docstring for why ``tripped`` stays ``None`` regardless of what
    comes back here."""
    prompt = (
        f"{SWEEP_ADVISORY_MARKER}\n"
        f"Decision: {claim.title or claim.slug} (claim {claim.id})\n"
        f"Reversal condition as written: {condition_text}\n"
        "This condition could not be checked mechanically against the plan or the regression "
        "parameters. In one or two sentences, note anything about its wording a human reviewer "
        "should look at when deciding whether it has tripped. Do not claim it has or has not "
        "tripped - only a human can decide that."
    )
    response = model.invoke(
        [
            SystemMessage(
                content=(
                    "You are assisting a brain-maintenance sweep. You never decide whether a "
                    "reversal condition has tripped - you only flag it for human review."
                )
            ),
            HumanMessage(content=prompt),
        ]
    )
    text = getattr(response, "content", "") or ""
    if isinstance(text, list):
        text = "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in text)
    return text.strip() or "(model returned no advisory note)"


def _evaluate_one(session: Session, claim: Claim, model: BaseChatModel | None) -> ReversalVerdict:
    text = (claim.reversal_condition or "").strip()
    title = claim.title or claim.slug

    m = _R2_RE.search(text)
    if m:
        result = _check_r_squared(session, m.group("code"), float(m.group("threshold")))
        if result is not None:
            tripped, detail = result
            return ReversalVerdict(claim.id, claim.slug, title, text, "r_squared", tripped, detail)

    m = _PLAN_VALUE_RE.search(text)
    if m:
        result = _check_plan_value(session, m.group("code"), int(m.group("year")), m.group("direction"), m.group("value"))
        if result is not None:
            tripped, detail = result
            return ReversalVerdict(claim.id, claim.slug, title, text, "plan_value", tripped, detail)

    # Neither template matched, or matched but named a category/year the plan does not actually
    # hold (both _check_* helpers return None in that case) - most PLATFORM.md §4.4 conditions
    # land here, because they are written as prose for a human. tripped stays None: an advisory
    # verdict is never a fact.
    detail = _advisory_pass(model, claim, text) if model is not None else "not mechanically evaluable; needs a human"
    return ReversalVerdict(claim.id, claim.slug, title, text, "advisory", None, detail)


def evaluate_reversal_conditions(session: Session, model: BaseChatModel | None = None) -> list[ReversalVerdict]:
    """One :class:`ReversalVerdict` per ``decided`` decision that carries a reversal condition
    (``Claim.reversal_condition``, indexed from "## What would reverse this" - PLATFORM.md §4.4).
    A decision without one at all cannot reach ``decided`` in the first place
    (``brainkit.validate``'s ``missing_reversal``), so every decided decision has exactly one
    verdict here. Pending/superseded decisions are not evaluated: a reversal condition only means
    anything once the decision it would reverse actually governs something (PLATFORM.md §12.5:
    "decided decisions whose reversal condition may have tripped")."""
    decided = (
        session.execute(
            select(Claim).where(
                Claim.kind == ClaimKind.decision,
                Claim.status == "decided",
                Claim.reversal_condition.is_not(None),
            ).order_by(Claim.id)
        )
        .scalars()
        .all()
    )
    return [_evaluate_one(session, c, model) for c in decided]


# --------------------------------------------------------------------------- deterministic checks


def _computed_derivation_findings(session: Session, brain_root: Path) -> tuple[Finding, ...]:
    """§12.6's own "orphaned links": a ``(computed, <key>)`` evidence tag whose derivation row is
    gone. Re-runs :func:`bridge.lookup.make_derivation_lookup` live rather than trusting
    ``Evidence.resolved`` (a snapshot from whenever this file was last reindexed - it does not
    know a derivation row was deleted since)."""
    lookup = make_derivation_lookup(session)
    findings: list[Finding] = []
    rows = session.execute(
        select(Evidence, Claim).join(Claim, Evidence.claim_id == Claim.id).where(Evidence.tag_kind == TagKind.computed)
    ).all()
    for evidence, claim in rows:
        row_parse = parse_row(evidence.tag_raw)
        key = row_parse.tags[0].derivation_key if row_parse.ok and row_parse.tags else None
        if not key or lookup(key) is not None:
            continue
        target = brain_root / claim.path if claim.path else brain_root
        findings.append(
            Finding(
                path=target,
                line=None,
                code=COMPUTED_DERIVATION_MISSING,
                message=f"claim {claim.id} ({claim.slug}) cites (computed, {key}) but no derivation row resolves it anymore",
                severity="warning",
            )
        )
    return tuple(findings)


def _supersession_findings(session: Session, brain_root: Path) -> tuple[Finding, ...]:
    """A decision marked ``superseded`` that is the target of no other decision's ``supersedes``
    link - the other half of that chain is missing (a human edited the status by hand, or the
    superseding file was later removed/renamed)."""
    findings: list[Finding] = []
    superseded = (
        session.execute(select(Claim).where(Claim.kind == ClaimKind.decision, Claim.status == "superseded"))
        .scalars()
        .all()
    )
    for claim in superseded:
        incoming = session.execute(
            select(ClaimLink).where(ClaimLink.relation == LinkRelation.supersedes, ClaimLink.to_claim_id == claim.id)
        ).first()
        if incoming is not None:
            continue
        target = brain_root / claim.path if claim.path else brain_root
        findings.append(
            Finding(
                path=target,
                line=None,
                code=BROKEN_SUPERSESSION_CHAIN,
                message=f"claim {claim.id} ({claim.slug}) is status=superseded but no decision's supersedes link points at it",
                severity="warning",
            )
        )
    return tuple(findings)


# --------------------------------------------------------------------------- the sweep itself


@dataclass(frozen=True)
class SweepReport:
    brain_root: Path
    structural_findings: tuple[Finding, ...]
    computed_derivation_findings: tuple[Finding, ...]
    supersession_findings: tuple[Finding, ...]
    reversal_verdicts: tuple[ReversalVerdict, ...]

    @property
    def tripped(self) -> tuple[ReversalVerdict, ...]:
        return tuple(v for v in self.reversal_verdicts if v.tripped is True)

    @property
    def advisory(self) -> tuple[ReversalVerdict, ...]:
        return tuple(v for v in self.reversal_verdicts if v.tripped is None)


def run_sweep(session: Session, brain_root: str | Path, *, model: BaseChatModel | None = None) -> SweepReport:
    """The sweep (PLATFORM.md §12.5/§12.6): read ``brain_root`` and the derived index, report
    what is stale or newly true, mutate nothing (see the module docstring's central guardrail).

    Reads the index as it stands - this does not reindex first. A caller that wants the sweep to
    see a just-written or just-edited file must reindex before calling this, exactly like
    ``bridge/check.py`` does before reading ``decided_effects``; the sweep itself performs no
    write of any kind, to the index or to a file, so it cannot be the thing that makes the index
    current.

    ``model`` defaults to ``resolve_model(None, provider=config.AI_BRAIN_WRITE_PROVIDER)`` - the
    same offline-by-default resolution every brain-writing entrypoint uses (B2/B3), even though
    this path writes nothing: the advisory pass can still make a real Anthropic call, so it must
    not go live just because a credential sits in ``.env`` - see
    ``config.AI_BRAIN_WRITE_PROVIDER``'s own docstring for why "auto" is wrong here."""
    brain_root = Path(brain_root)
    if model is None:
        model = resolve_model(None, provider=config.AI_BRAIN_WRITE_PROVIDER)

    all_structural = validate_tree(brain_root)
    structural = tuple(f for f in all_structural if f.code in REUSED_VALIDATE_CODES)

    return SweepReport(
        brain_root=brain_root,
        structural_findings=structural,
        computed_derivation_findings=_computed_derivation_findings(session, brain_root),
        supersession_findings=_supersession_findings(session, brain_root),
        reversal_verdicts=tuple(evaluate_reversal_conditions(session, model)),
    )


def render_sweep_report(report: SweepReport) -> str:
    """Human-readable rendering for the console script - ``nvplan.services.trace.render_trace``'s
    own role, one register down: turn a report dataclass into text a person reads, never a
    decision anything downstream acts on automatically."""
    lines = [f"sweep over {report.brain_root}"]

    def _findings(label: str, findings: Sequence[Finding]) -> None:
        lines.append(f"{label}: {len(findings)}")
        for f in findings:
            lines.append(f"  [{f.severity}] {f.code}: {f.path} :: {f.message}")

    _findings("structural findings (reused brainkit.validate codes)", report.structural_findings)
    _findings("computed-derivation findings", report.computed_derivation_findings)
    _findings("supersession findings", report.supersession_findings)

    lines.append(f"reversal conditions checked: {len(report.reversal_verdicts)}")
    for v in report.reversal_verdicts:
        if v.tripped is True:
            lines.append(f"  TRIPPED  {v.decision_slug} (claim {v.claim_id}) [{v.mechanism}]: {v.detail}")
        elif v.tripped is False:
            lines.append(f"  ok       {v.decision_slug} (claim {v.claim_id}) [{v.mechanism}]: {v.detail}")
        else:
            lines.append(
                f"  ADVISORY, needs a human  {v.decision_slug} (claim {v.claim_id}): "
                f"{v.condition_text!r} -- {v.detail}"
            )
    return "\n".join(lines)
