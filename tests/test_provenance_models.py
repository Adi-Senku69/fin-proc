import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from provenance.models import (
    Claim,
    ClaimKind,
    ClaimLink,
    Evidence,
    EvidenceSection,
    LinkRelation,
    get_engine,
    init_db,
)
from provenance.tags import TagKind


@pytest.fixture()
def engine():
    eng = get_engine("sqlite:///:memory:")
    init_db(eng)
    return eng


@pytest.fixture()
def session(engine):
    with Session(engine) as s:
        yield s


def _claim(session, **overrides):
    defaults = dict(kind=ClaimKind.decision, slug="drop-sso-tier")
    defaults.update(overrides)
    c = Claim(**defaults)
    session.add(c)
    session.flush()
    return c


# --------------------------------------------------------------------------- tables create


def test_tables_create_cleanly(engine):
    from sqlalchemy import inspect

    names = set(inspect(engine).get_table_names())
    assert {"claim", "evidence", "claim_link"} <= names


# --------------------------------------------------------------------------- evidence.claim_id NOT NULL


def test_evidence_requires_claim_id(session):
    session.add(
        Evidence(
            section=EvidenceSection.evidence_for,
            text="churn driven by missing SSO",
            tag_kind=TagKind.industry_knowledge,
            tag_raw="(industry-knowledge)",
            resolved=False,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_evidence_with_claim_id_ok(session):
    claim = _claim(session)
    session.add(
        Evidence(
            claim_id=claim.id,
            section=EvidenceSection.evidence_for,
            text="churn driven by missing SSO",
            tag_kind=TagKind.industry_knowledge,
            tag_raw="(industry-knowledge)",
            resolved=False,
        )
    )
    session.flush()
    fetched = session.scalar(select(Evidence).where(Evidence.claim_id == claim.id))
    assert fetched is not None
    assert fetched.section is EvidenceSection.evidence_for
    assert fetched.tag_kind is TagKind.industry_knowledge
    assert fetched.resolved is False


def test_evidence_dangling_claim_fk_rejected(session):
    session.add(
        Evidence(
            claim_id=999,
            section=EvidenceSection.not_doing,
            text="x",
            tag_kind=TagKind.chat,
            tag_raw="(chat, no artifact)",
            resolved=False,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


# --------------------------------------------------------------------------- claim_link uniqueness


def test_claim_link_triple_unique(session):
    a = _claim(session, slug="decision-a")
    b = _claim(session, slug="decision-b")
    session.add(ClaimLink(from_claim_id=a.id, to_claim_id=b.id, relation=LinkRelation.supersedes))
    session.flush()
    session.add(ClaimLink(from_claim_id=a.id, to_claim_id=b.id, relation=LinkRelation.supersedes))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_claim_link_same_pair_different_relation_ok(session):
    a = _claim(session, slug="decision-c")
    b = _claim(session, slug="decision-d")
    session.add(ClaimLink(from_claim_id=a.id, to_claim_id=b.id, relation=LinkRelation.supersedes))
    session.flush()
    session.add(ClaimLink(from_claim_id=a.id, to_claim_id=b.id, relation=LinkRelation.informs))
    session.flush()
    links = session.scalars(select(ClaimLink).where(ClaimLink.from_claim_id == a.id)).all()
    assert len(links) == 2


def test_claim_link_requires_valid_claim_ids(session):
    session.add(ClaimLink(from_claim_id=1, to_claim_id=999, relation=LinkRelation.tests))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


# --------------------------------------------------------------------------- claim.path unique


def test_claim_path_unique(session):
    _claim(session, slug="decision-e", path="decisions/2026-04-22-drop-sso.md")
    session.flush()
    session.add(Claim(kind=ClaimKind.decision, slug="decision-f", path="decisions/2026-04-22-drop-sso.md"))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_claim_path_nullable_and_multiple_nulls_ok(session):
    _claim(session, slug="decision-g", path=None)
    session.flush()
    session.add(Claim(kind=ClaimKind.decision, slug="decision-h", path=None))
    session.flush()  # two NULL paths must NOT violate the unique constraint (SQL NULL semantics)


# --------------------------------------------------------------------------- enum round-trips


def test_claim_kind_enum_round_trips(session):
    for kind in ClaimKind:
        c = Claim(kind=kind, slug=f"slug-{kind.value}")
        session.add(c)
    session.flush()
    session.expire_all()
    fetched_kinds = {c.kind for c in session.scalars(select(Claim)).all()}
    assert fetched_kinds == set(ClaimKind)


def test_evidence_section_and_tag_kind_enum_round_trip(session):
    claim = _claim(session)
    for section in EvidenceSection:
        for tag_kind in (TagKind.computed, TagKind.chat):
            session.add(
                Evidence(
                    claim_id=claim.id,
                    section=section,
                    text="x",
                    tag_kind=tag_kind,
                    tag_raw="(computed, k)" if tag_kind is TagKind.computed else "(chat, no artifact)",
                    resolved=True,
                )
            )
    session.flush()
    session.expire_all()
    rows = session.scalars(select(Evidence).where(Evidence.claim_id == claim.id)).all()
    assert {r.section for r in rows} == set(EvidenceSection)
    assert {r.tag_kind for r in rows} == {TagKind.computed, TagKind.chat}


def test_link_relation_enum_round_trips(session):
    a = _claim(session, slug="decision-i")
    b = _claim(session, slug="decision-j")
    for relation in LinkRelation:
        session.add(ClaimLink(from_claim_id=a.id, to_claim_id=b.id, relation=relation))
    session.flush()
    session.expire_all()
    fetched = {
        link.relation for link in session.scalars(select(ClaimLink).where(ClaimLink.from_claim_id == a.id)).all()
    }
    assert fetched == set(LinkRelation)


# --------------------------------------------------------------------------- claim defaults / fields


def test_claim_ingested_at_defaults_and_optional_fields(session):
    claim = _claim(session, slug="decision-k", title=None, status=None, date=None, body_sha256=None)
    session.flush()
    fetched = session.get(Claim, claim.id)
    assert fetched.ingested_at is not None
    assert fetched.derivation_id is None
    assert fetched.ai_record_id is None


def test_claim_holds_status_string_and_date(session):
    claim = _claim(session, slug="decision-l", status="decided", date=datetime.date(2026, 4, 22))
    session.flush()
    fetched = session.get(Claim, claim.id)
    assert fetched.status == "decided"
    assert fetched.date == datetime.date(2026, 4, 22)


def test_claim_derivation_and_ai_record_ids_are_plain_unconstrained_integers(session):
    """PLATFORM.md decoupling: these are NOT real FKs, so an arbitrary integer is accepted
    even though no nvplan.derivation/ai_record row with that id exists in this DB."""
    claim = _claim(session, slug="decision-m", derivation_id=123456, ai_record_id=654321)
    session.flush()  # must not raise
    fetched = session.get(Claim, claim.id)
    assert fetched.derivation_id == 123456
    assert fetched.ai_record_id == 654321


def test_evidence_target_ids_are_plain_unconstrained_integers(session):
    claim = _claim(session, slug="decision-n")
    ev = Evidence(
        claim_id=claim.id,
        section=EvidenceSection.evidence_for,
        text="x",
        tag_kind=TagKind.computed,
        tag_raw="(computed, some-key)",
        target_claim_id=999999,
        target_derivation_id=888888,
        resolved=False,
    )
    session.add(ev)
    session.flush()  # must not raise despite no matching rows existing
    assert ev.target_claim_id == 999999
    assert ev.target_derivation_id == 888888
