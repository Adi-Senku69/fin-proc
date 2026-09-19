"""P2 bridge (finance half, PLATFORM.md §7.1): trace reaches the decision.

A plan value carrying a ``claim_id`` gets a ``claim`` block beside the existing ``ai``
block, holding the decision title/status/decided-date, its evidence rows with their
provenance tags, and (when the index carries one) its reversal condition.

Two scenarios:

* a joint database (both nvplan's and provenance's metadata on one SQLite engine, exactly
  as ``bridge/db.py: init_platform_db`` will build it) - the claim block must be there;
* a finance-only database (nvplan's metadata alone, no claim/evidence tables at all) - the
  lookup must degrade gracefully: no claim block, and the trace must never raise.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from nvplan.config import DATA_DIR
from nvplan.db.models import Base as NvBase
from nvplan.db.session import SessionLocal, get_engine, init_db, seed_categories
from nvplan.ingest.actuals import ingest_actuals, load_actuals_csv
from nvplan.services.planning import Override, run_plan
from nvplan.services.trace import find_plan_value, render_trace, trace_plan_value

from brainkit.ingest import ingest_tree

from provenance import Claim, ClaimKind, Evidence, EvidenceSection, TagKind
from provenance.models import Base as ProvenanceBase

YEAR = 2027
DECISION_TITLE = "Ship EU region pricing"
DECISION_SLUG = "ship-eu-region-pricing"
TAG_A = "(stakeholder-verbal, Jane Doe, 2026-02-01)"
TAG_B = "(computed, param:REV)"

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_BRAIN_ROOT = REPO_ROOT / "brain"
REAL_REVERSAL_SLUG = "2026-09-20-sunset-legacy-import"


def _seed_finance(session: Session) -> None:
    seed_categories(session)
    ingest_actuals(session, load_actuals_csv(DATA_DIR / "actuals.csv"))


@pytest.fixture()
def joint_session():
    """One SQLite engine carrying BOTH metadatas - the shape ``bridge/db.py:
    init_platform_db`` guarantees, built directly here since ``bridge/`` is out of scope
    for this half of P2."""
    engine = get_engine("sqlite://")
    NvBase.metadata.create_all(engine)
    ProvenanceBase.metadata.create_all(engine)
    with SessionLocal(bind=engine) as s:
        _seed_finance(s)
        yield s


@pytest.fixture()
def finance_only_session():
    """A database created with ONLY nvplan's metadata - no claim/evidence tables exist."""
    engine = init_db(get_engine("sqlite://"))
    with SessionLocal(bind=engine) as s:
        _seed_finance(s)
        yield s


def _insert_claim_with_evidence(session: Session) -> Claim:
    claim = Claim(
        kind=ClaimKind.decision,
        slug=DECISION_SLUG,
        path=f"decisions/2026-02-15-{DECISION_SLUG}.md",
        title=DECISION_TITLE,
        status="decided",
        date=date(2026, 2, 15),
        body_sha256="0" * 64,
    )
    session.add(claim)
    session.commit()
    session.add_all(
        [
            Evidence(
                claim_id=claim.id,
                section=EvidenceSection.evidence_for,
                text="Interviewed EU customers confirmed willingness to pay the new tier.",
                tag_kind=TagKind.stakeholder_verbal,
                tag_raw=TAG_A,
                resolved=True,
            ),
            Evidence(
                claim_id=claim.id,
                section=EvidenceSection.evidence_for,
                text="Modelled revenue uplift from the finance plan's REV growth parameter.",
                tag_kind=TagKind.computed,
                tag_raw=TAG_B,
                resolved=True,
            ),
        ]
    )
    session.commit()
    return claim


def test_claim_block_reaches_trace_of_a_cost_value(joint_session):
    session = joint_session
    claim = _insert_claim_with_evidence(session)

    run_plan(
        session,
        revenue_override={YEAR: Override(value=21500.0, claim_id=claim.id, label=claim.slug)},
    )
    pers = find_plan_value(session, scenario_kind="base", category_code="PERS", year=YEAR)
    tree = trace_plan_value(session, pers.id)

    rev_node = tree.find(key=f"plan:base:REV:{YEAR}")
    assert rev_node is not None and rev_node.path == "decided"
    assert rev_node.claim is not None
    assert rev_node.claim["title"] == DECISION_TITLE
    assert rev_node.claim["slug"] == DECISION_SLUG
    assert rev_node.claim["status"] == "decided"
    assert rev_node.claim["decided_on"] == "2026-02-15"
    tags = {e["tag_raw"] for e in rev_node.claim["evidence"]}
    assert tags == {TAG_A, TAG_B}

    text = render_trace(tree)
    assert DECISION_TITLE in text
    assert TAG_A in text and TAG_B in text


def test_graceful_degradation_on_finance_only_database(finance_only_session):
    session = finance_only_session
    # A claim_id that no table anywhere can resolve - the point is the lookup itself, not
    # whether some other database happens to have a matching row.
    run_plan(
        session,
        revenue_override={YEAR: Override(value=21500.0, claim_id=4242, label="unresolvable-slug")},
    )
    pers = find_plan_value(session, scenario_kind="base", category_code="PERS", year=YEAR)

    # must not raise even though the claim/evidence tables don't exist at all
    tree = trace_plan_value(session, pers.id)
    rev_node = tree.find(key=f"plan:base:REV:{YEAR}")
    assert rev_node is not None and rev_node.path == "decided"
    assert rev_node.claim is None

    text = render_trace(tree)  # must not raise either
    assert "claim:" not in text


# --------------------------------------------------------------------------- reversal condition (PLATFORM.md §4.4)


def test_claim_block_carries_reversal_condition_when_indexed(joint_session):
    """A Claim whose reversal_condition column is populated shows the condition on its
    own indented line under the claim block, prefixed clearly as the reversal condition."""
    session = joint_session
    claim = _insert_claim_with_evidence(session)
    claim.reversal_condition = "If churn exceeds 5% quarter over quarter, we would revisit."
    session.commit()

    run_plan(
        session,
        revenue_override={YEAR: Override(value=21500.0, claim_id=claim.id, label=claim.slug)},
    )
    pers = find_plan_value(session, scenario_kind="base", category_code="PERS", year=YEAR)
    tree = trace_plan_value(session, pers.id)

    rev_node = tree.find(key=f"plan:base:REV:{YEAR}")
    assert rev_node.claim["reversal_condition"] == claim.reversal_condition

    text = render_trace(tree)
    assert "reversal condition: If churn exceeds 5%" in text


def test_claim_block_omits_reversal_line_when_none(joint_session):
    """A decision with no (or an empty) '## What would reverse this' section indexes
    reversal_condition as None; the trace must render with no crash and no line for it -
    graceful degradation must hold for a missing reversal exactly like a missing claim."""
    session = joint_session
    claim = _insert_claim_with_evidence(session)  # reversal_condition left at its default: None
    assert claim.reversal_condition is None

    run_plan(
        session,
        revenue_override={YEAR: Override(value=21500.0, claim_id=claim.id, label=claim.slug)},
    )
    pers = find_plan_value(session, scenario_kind="base", category_code="PERS", year=YEAR)
    tree = trace_plan_value(session, pers.id)  # must not raise

    rev_node = tree.find(key=f"plan:base:REV:{YEAR}")
    assert rev_node.claim["reversal_condition"] is None

    text = render_trace(tree)  # must not raise
    assert "reversal condition:" not in text


def test_reversal_condition_round_trips_from_markdown_to_render_trace(joint_session):
    """Full pipeline, real worked example (PLATFORM.md §4.4): brainkit.ingest indexes
    brain/decisions/2026-09-20-sunset-legacy-import.md's '## What would reverse this'
    prose onto its Claim, and nvplan.services.trace prints it in the trace of a
    downstream plan value the decision's revenue override touches."""
    session = joint_session
    report = ingest_tree(session, REAL_BRAIN_ROOT, strict=True)
    assert report.rejected == ()
    claim = session.execute(select(Claim).where(Claim.slug == REAL_REVERSAL_SLUG)).scalar_one()
    assert claim.reversal_condition is not None

    run_plan(
        session,
        revenue_override={YEAR: Override(value=22900.0, claim_id=claim.id, label=claim.slug)},
    )
    pers = find_plan_value(session, scenario_kind="base", category_code="PERS", year=YEAR)
    tree = trace_plan_value(session, pers.id)
    text = render_trace(tree)

    assert "reversal condition:" in text
    assert "10 currently-active" in text
