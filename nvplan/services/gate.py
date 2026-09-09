"""The human confirmation gate (PDF day 10).

"Nothing enters the plan unconfirmed; rejection leaves no trace in figures."

* :func:`confirm_proposal` flips an ``ai_record`` from ``proposed`` to
  ``confirmed`` (who / when) and reruns the plan with the proposed revenue as
  the base path for that year, so the whole cascade (costs, statements) is
  recomputed and every affected number carries the ``ai_record_id`` in its
  lineage. Confirming anything that is not ``proposed`` raises - no
  double-confirm, no confirming a rejection.
* :func:`reject_proposal` flips it to ``rejected``. No plan run, no plan value
  ever references the record (asserted).

Both touch only the ``ai_record`` row (status, confirmed_by, confirmed_at) and,
via :func:`run_plan`, insert new rows; nothing existing is updated. The model
has no ``rejected_by`` column, so for a rejection ``confirmed_by`` /
``confirmed_at`` hold the rejecting user and time - the status says which
decision it was.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nvplan.db.models import AiRecord, AiStatus, PlanValue, Touchpoint
from nvplan.services.planning import PlanRun, run_plan

__all__ = ["confirm_proposal", "reject_proposal", "GateError"]


class GateError(ValueError):
    """The record cannot be confirmed / rejected in its current state."""


def _load_proposal(session: Session, ai_record_id: int) -> AiRecord:
    rec = session.get(AiRecord, ai_record_id)
    if rec is None:
        raise LookupError(f"ai_record {ai_record_id} not found")
    if rec.status is not AiStatus.proposed:
        raise GateError(f"ai_record {ai_record_id} is {rec.status.value}, not proposed - decision already taken")
    return rec


def confirm_proposal(
    session: Session,
    ai_record_id: int,
    *,
    confirmed_by: str,
    rerun: bool = True,
    **run_kwargs,
) -> PlanRun | None:
    """Confirm a revenue proposal and (by default) rerun the plan with it.

    Returns the :class:`PlanRun` of the rerun, or ``None`` when ``rerun=False``
    (the record is confirmed but nothing enters ``plan_value`` until a run
    with ``revenue_override`` is made). ``run_kwargs`` are passed to
    :func:`run_plan` (e.g. ``data_dir``).
    """
    if not confirmed_by:
        raise GateError("confirmed_by is required: a human must sign the confirmation")
    rec = _load_proposal(session, ai_record_id)
    if rec.touchpoint is not Touchpoint.revenue_proposal:
        raise GateError(f"ai_record {ai_record_id} is a {rec.touchpoint.value}, only revenue proposals enter the plan")
    if rec.year is None or rec.proposed_value is None:
        raise GateError(f"ai_record {ai_record_id} has no year / proposed_value")

    rec.status = AiStatus.confirmed
    rec.confirmed_by = confirmed_by
    rec.confirmed_at = datetime.now(timezone.utc)
    session.flush()

    if not rerun:
        session.commit()
        return None
    try:
        return run_plan(
            session,
            created_by=confirmed_by,
            revenue_override={int(rec.year): (float(rec.proposed_value), int(rec.id))},
            label_suffix=f" (AI proposal #{rec.id} confirmed by {confirmed_by})",
            **run_kwargs,
        )
    except Exception:
        session.rollback()  # run_plan rolled back already; make sure the status flip goes with it
        raise


def reject_proposal(session: Session, ai_record_id: int, *, rejected_by: str) -> AiRecord:
    """Reject a proposal. No plan run; asserts no plan value references the record."""
    if not rejected_by:
        raise GateError("rejected_by is required")
    rec = _load_proposal(session, ai_record_id)
    rec.status = AiStatus.rejected
    rec.confirmed_by = rejected_by
    rec.confirmed_at = datetime.now(timezone.utc)
    session.flush()
    n = session.scalar(select(func.count(PlanValue.id)).where(PlanValue.ai_record_id == rec.id))
    if n:
        session.rollback()
        raise GateError(f"ai_record {ai_record_id} is referenced by {n} plan values - cannot reject a value in use")
    session.commit()
    return rec
