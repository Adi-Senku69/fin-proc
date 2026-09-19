"""The conversational assistant (UI.md Part 3): the backstop scan, the verification pass,
and the endpoint - all run offline against the scripted fake model."""

from __future__ import annotations

import json
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from bridge.db import init_platform_db
from nvplan.ai import AnswerRejected, ask, loose_figures
from nvplan.ai.fake import scripted_assistant_model
from nvplan.api.app import create_app
from nvplan.db.models import AiRecord, PlanValue, Touchpoint
from nvplan.db.session import category_map, get_engine, init_db
from provenance.models import Claim, ClaimKind
from test_ai_fixtures import PLAN_YEAR, seed_ai_db  # noqa: F401 (reused seeding helper)


@pytest.fixture()
def assistant_db(tmp_path):
    """A file-based SQLite with both nvplan and provenance tables (like ``bridge.db``),
    seeded like ``test_ai_fixtures.ai_db`` plus one decided claim with a quantified effect -
    enough to exercise a "claim" figure/segment citation without a real ``brain/`` tree."""
    engine = get_engine(f"sqlite:///{tmp_path / 'assistant_test.db'}", connect_args={"check_same_thread": False})
    init_platform_db(init_db(engine))
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        plans = seed_ai_db(s)
        claim = Claim(
            kind=ClaimKind.decision,
            slug="2026-09-20-sunset-legacy-import",
            title="Sunset the legacy import",
            status="decided",
            date=date(2026, 9, 20),
            effect_json={"category": "REV", "year": 2027, "value": 22_900.0, "unit": "kEUR"},
        )
        s.add(claim)
        s.commit()
        cats = category_map(s)
        rev_pv = s.scalar(
            select(PlanValue).where(PlanValue.category_id == cats["REV"].id, PlanValue.year == PLAN_YEAR)
        )
        ids = {"claim_id": claim.id, "plan_value_id": rev_pv.id, "plan_value_value": rev_pv.value}
    return factory, plans, ids


@pytest.fixture()
def no_credentials(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("nvplan.ai.agents._ant_cli", lambda: None)
    return None


def _records(factory) -> list[AiRecord]:
    with factory() as s:
        return list(s.scalars(select(AiRecord)).all())


# --------------------------------------------------------------------------- the backstop scan (pure function)
#
# One parametrized table for the whole contract: every shape the scan must reject, every shape
# it must let through, and (grouped at the end) the three escapes this fix closes. See
# nvplan.ai.assistant's shape comments (_is_identifier_shape and friends) for the reasoning
# behind each masked shape, and the module's "try to break it" residuals below the table.
LOOSE_FIGURES_CASES: list[tuple[str, list[str]]] = [
    # -- must still reject (money, percentages, negatives, bare numbers, out-of-range years,
    #    the German decimal form) --------------------------------------------------------------
    ("Costs came in around €1,245.7 k this year.", ["1,245.7"]),
    ("Margin improved by 12.5% year over year.", ["12.5%"]),
    ("A plain 500 k EUR swing.", ["500"]),
    ("A negative -245.7 swing.", ["-245.7"]),
    ("A negative −245.7 swing.", ["−245.7"]),  # unicode minus (U+2212), not ASCII hyphen
    ("500 categories would not be a count.", ["500"]),
    ("A year outside both windows, 2031, is not free.", ["2031"]),
    # German formatting (dot as thousands, comma as decimal): the "5" after the comma used to
    # read as an allowed small count (the pre-existing "5 EUR" gap, now closed - "EUR" is a unit
    # word, so both pieces reject).
    ("betrug 22.900,5 EUR", ["22.900", "5"]),
    ("param:PERS moved by 4.5% though.", ["4.5%"]),  # the identifier is masked; the figure beside it is not
    # -- must still pass (years in window/horizon, year ranges, small counts, identifiers,
    #    a full decision slug, "54-position", "Q1 2027", spelled-out counts) -------------------
    ("In 2025 actuals landed above plan.", []),
    ("The horizon runs 2026-2030.", []),
    ("There are 3 scenarios in the grid.", []),
    ("There are three scenarios in the grid.", []),
    ("See param:PERS for the cost driver.", []),
    ("The decision 2026-09-20-sunset-legacy-import is decided.", []),
    ("the 54-position framework", []),
    ("Q1 2027 numbers", []),
    ("D2.P4 Wage growth", []),
    ("", []),
    ("No numbers here at all.", []),
    # -- the three escapes this fix closes (decimal with no leading integer part, scientific
    #    notation, digits immediately fused to a unit) ------------------------------------------
    ("beta is .488", [".488"]),
    ("revenue is 1e5", ["1e5"]),
    ("1E-5 tolerance", ["1E-5"]),
    ("revenue is 22900kEUR", ["22900kEUR"]),
    # -- evasions tried and closed while rewriting the masking rule (see the module's shape
    #    comments for why each of these would otherwise have slipped through) -----------------
    ("the deviation was 22900-legacy-import kEUR", ["22900"]),  # fabricated 3-part "slug"
    ("revenue moved by 500-up-again this year", ["500"]),  # fabricated 3-part "slug", smaller figure
    ("revenue:22900 is the code", ["22900"]),  # a fake colon-prefixed "identifier"
    ("(500) in parens", ["500"]),  # parentheses don't shield a bare number
    ("500", ["500"]),  # a number alone, at the very start of the segment
    ("swing of 500", ["500"]),  # a number alone, at the very end of the segment
    ("22,,900 repeated separator", ["22", "900"]),  # a doubled separator still yields two offenders
    # -- the two remaining holes this fix closes: the count exemption swallowing a spelled-out
    #    unit ("15 percent" instead of "15%"), and a figure fused to a unit written BEFORE it
    #    ("USD22900", the mirror of the trailing-fusion case above) --------------------------
    ("costs are 5 EUR", ["5"]),
    ("that is 12 percent", ["12"]),
    ("margin of 15 percent", ["15"]),
    ("margin of 15 percent.", ["15"]),  # punctuation right after the unit word doesn't shield it
    ("a swing of 8 per cent this year", ["8"]),
    ("tightened by 3 basis points", ["3"]),
    ("widened by 4 bps", ["4"]),
    ("grew 2 x versus last year", ["2"]),
    ("costs are 5 kEUR", ["5"]),
    ("revenue is USD22900 this year", ["USD22900"]),
    ("revenue is kEUR22900 this year", ["kEUR22900"]),
    ("code FY1234 is not a real year", ["FY1234"]),  # 4-digit fusion, but not a valid year - caught
    # -- must still pass: genuine counts (a real word, not a unit) and short/valid fused labels --
    ("about 15 accounts were affected", []),
    ("5 categories changed", []),
    ("FY2025 results were strong", []),  # 4-digit fusion, valid year in window - exempted
    ("the value was q15 exactly", []),  # 2-digit fusion - exempted (see residual test below)
]


@pytest.mark.parametrize("text,expected", LOOSE_FIGURES_CASES)
def test_loose_figures_contract(text, expected):
    assert loose_figures(text) == expected


# --------------------------------------------------------------------------- residual escapes, documented
#
# These are NOT required to fail by the brief; each is a real, tried evasion that a bounded,
# explicit-shape scanner still cannot close without either breaking a required "must pass" case
# or reaching beyond what this backstop scan is for (a full grammar of natural-language number
# formatting). Documented here, deliberately, rather than silently left for someone else to
# rediscover - each assertion pins the CURRENT (accepted) behaviour so a future change to the
# masking rule that accidentally widens the gap will be caught by this test failing.


def test_residual_letter_prefixed_fusion_is_now_caught_except_short_or_valid_year():
    """Closed: a unit written BEFORE the figure, fused with no space ("USD22900", "kEUR22900"),
    is now caught by ``_leading_fusions`` even though ``_NUMERIC_RE``'s own leading boundary still
    refuses to start a match right after a letter (that boundary was deliberately NOT widened -
    see its comment). What remains exempt, by design, is a digit run short enough to be a real
    label (<= 2 digits, "Q1"/"D2" - see ``test_residual_short_letter_prefixed_label_is_not_
    caught`` below) or a four-digit run that IS a valid year in window/horizon ("FY2025",
    "FY2027") - a four-digit run that ISN'T a valid year ("FY1234") is not exempt and is caught."""
    assert loose_figures("revenue is USD22900 this year") == ["USD22900"]
    assert loose_figures("revenue is kEUR22900 this year") == ["kEUR22900"]
    assert loose_figures("FY2025 results were strong") == []  # valid year in window - still exempt
    assert loose_figures("code FY1234 is not a real year") == ["FY1234"]  # not a valid year - caught


def test_residual_short_letter_prefixed_label_is_not_caught():
    """The label shape ("Q1", "D2.P4") caps its digit run at two digits specifically to block a
    large fused figure ("q1500") from posing as a label - see test below. ``_leading_fusions``
    uses that same two-digit cap as its own exemption, so a SHORT number behind a single-letter
    prefix ("q15") is deliberately still let through - not a hole in the label shape, but the
    same "real labels never need more than two digits" trade-off applied one level down."""
    assert loose_figures("the value was q15 exactly") == []


def test_large_fused_pseudo_label_is_rejected_not_masked():
    """Confirms the label shape's two-digit cap does its job: a large number dressed up with a
    single-letter prefix does NOT get masked as a label - it is not wrongly treated as a safe
    named identifier the way "Q1" is. Now that ``_leading_fusions`` catches a leading fusion
    whose digit run is neither short (<= 2 digits) nor a valid year, it IS flagged as an
    offender too (its 4-digit run, "1500", is not a year in window/horizon)."""
    from nvplan.ai.assistant import _is_identifier_shape

    assert _is_identifier_shape("q1500") is False
    assert loose_figures("the value was q1500 exactly") == ["q1500"]


def test_residual_colon_and_iso_date_year_slot_are_unbounded():
    """Both remaining "mask only" shapes leave one slot unvalidated by design: a colon token's
    numeric segment is only checked for LENGTH (1, 2, or 4 digits - a year or a short index), and
    an ISO date's year slot isn't range-checked at all (a decision may predate the plan horizon).
    Either can therefore smuggle a value up to 4 digits (9999) - far smaller than the arbitrary,
    unbounded figure the pre-fix heuristic let through, and the closest a bounded, explicit-shape
    scanner gets without hard-coding a business-specific year range into a generic parser."""
    assert loose_figures("plan:base:REV:9999 is the key") == []  # 4-digit colon segment, unvalidated
    assert loose_figures("dated 1234-06-15 exactly") == []  # syntactically valid, implausible year


def test_residual_hyphen_compound_label_is_capped_not_eliminated():
    """"54-position" must read clean (UI.md Part 3's own example), and a hyphen-fused compound
    is the only shape that fits it; capping the digit run at 3 digits keeps the smuggled amount
    under 1000 rather than leaving it open-ended, but a number under that cap ("500-widgets")
    still passes - a smaller, capped version of the same trade-off "54-position" requires."""
    assert loose_figures("the 54-position framework") == []
    assert loose_figures("500-widgets shipped") == []  # accepted residual: bounded to < 1000
    assert loose_figures("22900-widgets shipped") == ["22900"]  # above the cap: rejected, not masked


def test_residual_small_count_unit_word_is_a_named_list_not_a_grammar():
    """Closed for the units named in the brief: ``_is_allowed_count`` now denies the exemption
    when the following word is one of a finite, named list of units/measures ("percent", "EUR",
    "k", ...; see ``_COUNT_UNIT_WORDS``/``_COUNT_UNIT_PHRASES``). "The cost was 5 EUR." and "The
    cost was 12 kEUR." - the two examples the pre-fix gap was pinned on - are now both rejected
    (moved into ``LOOSE_FIGURES_CASES`` as "costs are 5 EUR" / "costs are 5 kEUR"). What remains,
    by construction, is a unit word OUTSIDE that list: this is a named-list gap, not a grammar of
    "what counts as a unit", so a currency this list doesn't name still reads as a plain count."""
    assert loose_figures("The cost was 5 EUR.") == ["5"]
    assert loose_figures("The cost was 12 kEUR.") == ["12"]
    assert loose_figures("The cost was 5 GBP.") == []  # GBP is not in the named unit list


# --------------------------------------------------------------------------- Touchpoint.assistant is additive


def test_assistant_touchpoint_member_leaves_existing_records_readable(assistant_db):
    """``Touchpoint`` is stored by member NAME (``Enum(Touchpoint)``, nvplan/db/models.py), so
    adding ``assistant`` must not change how a row written under one of the three pre-existing
    touchpoints reads back. Write a row exactly as pre-fix code would (naming an old member) and
    confirm it still round-trips to that same member, unaffected by the new one existing."""
    factory, plans, ids = assistant_db
    with factory() as s:
        for tp in (Touchpoint.env_scan, Touchpoint.revenue_proposal, Touchpoint.deviation_explanation):
            s.add(
                AiRecord(
                    touchpoint=tp,
                    prompt_text="p",
                    response_text="r",
                    rationale="pre-existing row",
                    model_version="claude-opus-5",
                )
            )
        s.commit()
    with factory() as s:
        recs = list(s.scalars(select(AiRecord).order_by(AiRecord.id)).all())
    assert [r.touchpoint for r in recs] == [
        Touchpoint.env_scan,
        Touchpoint.revenue_proposal,
        Touchpoint.deviation_explanation,
    ]


# --------------------------------------------------------------------------- ask(): verify-before-persist


def test_ask_well_formed_answer_verifies_and_persists_one_record(assistant_db):
    factory, plans, ids = assistant_db
    segments = [
        {"type": "text", "text": f"In {PLAN_YEAR + 1} the plan for revenue is:"},
        {
            "type": "figure",
            "label": "Revenue plan",
            "value": ids["plan_value_value"],
            "unit": "k EUR",
            "ref": {"kind": "plan_value", "id": ids["plan_value_id"]},
        },
        {"type": "claim", "claim_id": ids["claim_id"], "title": "Sunset the legacy import", "status": "decided"},
    ]
    model = scripted_assistant_model(segments=segments)

    answer = ask(factory, "What is the revenue plan, and what decision affects it?", model=model)

    assert answer.ai_record_id > 0
    assert [s.type for s in answer.segments] == ["text", "figure", "claim"]
    assert answer.usage == {}  # the fake model reports no usage_metadata
    recs = _records(factory)
    assert len(recs) == 1
    rec = recs[0]
    assert rec.id == answer.ai_record_id
    # The assistant gets its own touchpoint (Fix 2): no more reusing deviation_explanation with a
    # "[assistant] " prefix on the rationale - the rationale is the question itself, verbatim.
    assert rec.touchpoint is Touchpoint.assistant
    assert rec.rationale == "What is the revenue plan, and what decision affects it?"
    assert rec.status.value == "proposed"
    assert "---USER---" in rec.prompt_text
    assert "---STRUCTURED---" in rec.response_text
    assert rec.call_log_json is not None


def test_figure_value_disagreeing_with_row_is_rejected_and_nothing_persisted(assistant_db):
    factory, plans, ids = assistant_db
    segments = [
        {
            "type": "figure",
            "label": "Revenue plan",
            "value": ids["plan_value_value"] + 5.0,
            "unit": "k EUR",
            "ref": {"kind": "plan_value", "id": ids["plan_value_id"]},
        }
    ]
    model = scripted_assistant_model(segments=segments)

    with pytest.raises(AnswerRejected, match="vs stored"):
        ask(factory, "What is the revenue plan?", model=model)
    assert _records(factory) == []


def test_dangling_ref_is_rejected_and_nothing_persisted(assistant_db):
    factory, plans, ids = assistant_db
    segments = [
        {"type": "figure", "label": "Ghost", "value": 1.0, "unit": "k EUR", "ref": {"kind": "plan_value", "id": 999999}}
    ]
    model = scripted_assistant_model(segments=segments)

    with pytest.raises(AnswerRejected, match="does not resolve"):
        ask(factory, "q", model=model)
    assert _records(factory) == []


def test_loose_money_figure_in_prose_is_rejected(assistant_db):
    factory, plans, ids = assistant_db
    segments = [{"type": "text", "text": "Costs came in around €1,245.7 k this year, well above plan."}]
    model = scripted_assistant_model(segments=segments)

    with pytest.raises(AnswerRejected, match=r"1,245\.7"):
        ask(factory, "how are costs?", model=model)
    assert _records(factory) == []


def test_loose_percentage_figure_in_prose_is_rejected(assistant_db):
    factory, plans, ids = assistant_db
    segments = [{"type": "text", "text": "Margin improved by 12.5% year over year."}]
    model = scripted_assistant_model(segments=segments)

    with pytest.raises(AnswerRejected, match="12.5%"):
        ask(factory, "how is margin?", model=model)
    assert _records(factory) == []


def test_year_in_prose_is_allowed(assistant_db):
    factory, plans, ids = assistant_db
    segments = [{"type": "text", "text": f"Actuals through {PLAN_YEAR} are loaded; nothing else to report."}]
    model = scripted_assistant_model(segments=segments)

    answer = ask(factory, "what actuals are loaded?", model=model)
    assert answer.segments[0].text.strip()
    assert len(_records(factory)) == 1


def test_identifier_and_decision_slug_in_prose_are_allowed(assistant_db):
    factory, plans, ids = assistant_db
    segments = [
        {
            "type": "text",
            "text": "param:PERS is the cost line that decision 2026-09-20-sunset-legacy-import affects.",
        }
    ]
    model = scripted_assistant_model(segments=segments)

    answer = ask(factory, "what does the decision affect?", model=model)
    assert len(answer.segments) == 1
    assert len(_records(factory)) == 1


def test_claim_segment_with_bad_id_is_rejected(assistant_db):
    factory, plans, ids = assistant_db
    segments = [{"type": "claim", "claim_id": 424242, "title": "Nonexistent", "status": "decided"}]
    model = scripted_assistant_model(segments=segments)

    with pytest.raises(AnswerRejected, match="does not exist"):
        ask(factory, "q", model=model)
    assert _records(factory) == []


def test_parameter_figure_matches_any_of_its_fields(assistant_db):
    """A "parameter" ref has no single value column; it passes when the figure matches ANY
    of alpha/beta/R^2/valorization_rate within tolerance."""
    from nvplan.ai.tools import make_read_tools

    factory, plans, ids = assistant_db
    tool = {t.name: t for t in make_read_tools(factory)}["get_parameters"]
    row = next(r for r in json.loads(tool.invoke({}))["rows"] if r["category_code"] == "MAT")
    segments = [
        {
            "type": "figure",
            "label": "MAT beta",
            "value": row["beta"],
            "unit": "ratio",
            "ref": {"kind": "parameter", "id": row["parameter_id"]},
        }
    ]
    model = scripted_assistant_model(segments=segments)

    answer = ask(factory, "what is MAT's beta?", model=model)
    assert len(answer.segments) == 1
    assert len(_records(factory)) == 1


# --------------------------------------------------------------------------- tools return citable ids


def test_every_new_tool_returns_the_id_needed_to_cite_it(assistant_db):
    from nvplan.ai.tools import make_read_tools

    factory, plans, ids = assistant_db
    tools = {t.name: t for t in make_read_tools(factory)}

    pv = json.loads(tools["get_plan_value"].invoke({"scenario_kind": "base", "category_code": "REV", "year": PLAN_YEAR}))
    assert pv["plan_value_id"] == ids["plan_value_id"]

    grid = json.loads(tools["get_plan_values"].invoke({"scenario_kind": "base"}))
    assert all("plan_value_id" in r for r in grid["rows"])

    params = json.loads(tools["get_parameters"].invoke({}))
    assert all("parameter_id" in r for r in params["rows"])

    # every category's plan_value shares one derivation in this fixture (test_ai_fixtures.seed_ai_db);
    # regardless of which one the lineage lookup resolves to, its own kind carries its own id.
    trace = json.loads(tools["get_trace"].invoke({"plan_value_id": ids["plan_value_id"]}))
    assert trace["kind"] == "plan_value" and isinstance(trace["ref_id"], int)

    decisions = json.loads(tools["get_decisions"].invoke({}))
    assert any(r["claim_id"] == ids["claim_id"] for r in decisions["rows"])

    one = json.loads(tools["get_decision"].invoke({"claim_id": ids["claim_id"]}))
    assert one["claim_id"] == ids["claim_id"] and one["effect"]["value"] == pytest.approx(22_900.0)

    impact = json.loads(tools["get_claim_impact"].invoke({"claim_id": ids["claim_id"]}))
    assert impact == {"rows": []}  # this claim drove no plan value in this fixture

    backtest = json.loads(tools["get_backtest_summary"].invoke({}))
    assert "error" in backtest or "summary" in backtest  # context-only; no id expected


# --------------------------------------------------------------------------- endpoint


def test_endpoint_returns_422_with_failures_on_rejection(assistant_db):
    factory, plans, ids = assistant_db
    app = create_app(session_factory=factory)
    bad_segments = [{"type": "text", "text": "Costs were 45% higher than plan."}]
    app.state.model_factory = lambda tp, ctx: scripted_assistant_model(segments=bad_segments)  # noqa: ARG005
    with TestClient(app) as client:
        r = client.post("/assistant/ask", json={"question": "how are costs?"})
        assert r.status_code == 422, r.text
        assert "45%" in r.json()["detail"]
        assert client.get("/ai/records").json() == []


def test_endpoint_returns_200_with_segments_and_usage_on_success(assistant_db):
    factory, plans, ids = assistant_db
    app = create_app(session_factory=factory)
    good_segments = [
        {
            "type": "figure",
            "label": "Revenue plan",
            "value": ids["plan_value_value"],
            "unit": "k EUR",
            "ref": {"kind": "plan_value", "id": ids["plan_value_id"]},
        }
    ]
    app.state.model_factory = lambda tp, ctx: scripted_assistant_model(segments=good_segments)  # noqa: ARG005
    with TestClient(app) as client:
        r = client.post("/assistant/ask", json={"question": "what is revenue?"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["segments"][0]["type"] == "figure"
        assert body["segments"][0]["ref"] == {"kind": "plan_value", "id": ids["plan_value_id"]}
        assert body["ai_record_id"] > 0
        assert body["usage"] == {}
        assert len(client.get("/ai/records").json()) == 1


def test_endpoint_503_without_model_or_credentials(assistant_db, no_credentials):
    factory, plans, ids = assistant_db
    app = create_app(session_factory=factory)  # no model_factory injected
    with TestClient(app) as client:
        r = client.post("/assistant/ask", json={"question": "what is revenue?"})
        assert r.status_code == 503, r.text
        assert "ANTHROPIC_API_KEY" in r.json()["detail"]
        assert client.get("/ai/records").json() == []
