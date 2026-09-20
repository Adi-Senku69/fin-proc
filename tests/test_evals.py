"""Work package A3: the property-based eval harness runs under the normal offline suite.

Two concerns, matching tests/test_reachability.py's own framing ("a case that is written and
then silently never run" is the exact failure mode this guards against):

1. Reachability - every case nvplan.ai.evals.cases.CASES registers is actually executed by
   the runner (no case silently dropped), and every declared injection channel is exercised
   by at least one registered case (no channel silently unused).
2. The properties themselves hold - every case not listed in KNOWN_HOLES passes
   (``test_non_hole_cases_all_pass``). A case that WAS a real, documented hole
   (``assistant.claim_status_forgery_not_flagged``) is now fixed (see
   ``test_claim_status_forgery_is_now_caught``) and is directly pinned rather than left to a
   strict xfail, so a regression fails loudly here regardless of whether
   ``nvplan.ai.evals.cases.KNOWN_HOLES`` (a different package's file) has been updated to
   drop the now-stale entry.

All of this runs entirely offline (scripted FakeToolCallingModel, sqlite files under
tmp_path) - no live API call, no credential - so it runs under the default
``uv run pytest`` (``-m 'not live'``), never only under ``-m live``.
"""

from __future__ import annotations

import pytest

from nvplan.ai.evals import CASES, INJECTION_CHANNELS, KNOWN_HOLES, run_all
from nvplan.ai.evals.runner import format_report


@pytest.fixture(scope="module")
def outcomes(tmp_path_factory):
    """Run the whole registry exactly once per test module run; every test below inspects
    this same result rather than re-running eleven agent invocations per assertion."""
    tmp_root = tmp_path_factory.mktemp("evals")
    return run_all(tmp_root)


# --------------------------------------------------------------------------- registry sanity


def test_case_registry_has_unique_non_empty_names():
    assert CASES, "the eval registry must not be empty"
    names = [c.name for c in CASES]
    assert len(names) == len(set(names)), f"duplicate case name(s): {sorted(n for n in names if names.count(n) > 1)}"


def test_known_holes_reference_real_registered_cases():
    """KNOWN_HOLES must name cases that actually exist - a stale entry here would silently stop
    covering anything."""
    case_names = {c.name for c in CASES}
    stale = KNOWN_HOLES - case_names
    assert not stale, f"KNOWN_HOLES names case(s) not in the registry: {sorted(stale)}"


# --------------------------------------------------------------------------- 1: reachability


def test_every_registered_case_is_executed_by_the_runner(outcomes):
    """The exact miss tests/test_reachability.py exists to catch, applied to this registry: a
    case built and never run would sit here untested forever. run_all must produce exactly one
    outcome per registered case, no fewer, no more."""
    executed = {o.case.name for o in outcomes}
    registered = {c.name for c in CASES}
    assert executed == registered, (
        f"case(s) registered but never executed by run_all: {sorted(registered - executed)}; "
        f"outcome(s) with no matching registration: {sorted(executed - registered)}"
    )
    assert len(outcomes) == len(CASES), "run_all must return exactly one outcome per case"


def test_every_injection_channel_is_exercised_by_at_least_one_case():
    """Both directions, like tests/test_reachability.py's tool checks: a channel declared in
    INJECTION_CHANNELS but probed by no case would be a documented gap nobody actually tests;
    a case naming a channel not in INJECTION_CHANNELS would be an undocumented one."""
    declared = set(INJECTION_CHANNELS)
    used = {c.channel for c in CASES}
    orphan_channels = declared - used
    undeclared_channels = used - declared
    assert not orphan_channels, f"channel(s) declared in INJECTION_CHANNELS but exercised by no case: {sorted(orphan_channels)}"
    assert not undeclared_channels, f"case(s) name a channel not declared in INJECTION_CHANNELS: {sorted(undeclared_channels)}"


def test_the_b2_ingestion_channel_is_registered_and_actually_exercised(outcomes):
    """PLATFORM.md phase B2 (ingestion) is now built (see test_the_b2_extension_seam_is_registered_but_not_built
    in this file's own history for the pin this replaces): brain_ingestion.assistant is a real,
    tested channel now, not just a reserved name. This still guards the same property the old
    pin guarded - a declared channel must not silently claim coverage it lacks - by checking the
    positive claim in full rather than trusting the label: the channel is declared, at least one
    registered case names it, and that case is one of the outcomes run_all actually produced (not
    merely declared and skipped), and it passes."""
    assert "brain_ingestion.assistant" in INJECTION_CHANNELS
    # brain_write.b2_seam (the B1 substrate any B2 writer inherits) was already registered and
    # exercised before B2 landed; it stays registered and unaffected by B2 shipping.
    assert "brain_write.b2_seam" in INJECTION_CHANNELS

    channel_cases = {c.name for c in CASES if c.channel == "brain_ingestion.assistant"}
    assert channel_cases, "brain_ingestion.assistant is declared but no registered case names it"

    executed = {o.case.name: o for o in outcomes if o.case.name in channel_cases}
    assert executed.keys() == channel_cases, (
        f"case(s) for brain_ingestion.assistant registered but never executed by run_all: "
        f"{sorted(channel_cases - executed.keys())}"
    )

    failures = [o for o in executed.values() if not o.passed]
    assert not failures, "brain_ingestion.assistant case(s) failed:\n" + format_report(failures)


def test_the_b3_assistant_draft_channel_is_registered_and_actually_exercised(outcomes):
    """PLATFORM.md phase B3 (drafting decisions/hypotheses from a question) is now built: the
    assistant's draft_decision/draft_hypotheses tools (nvplan.ai.tools, via bridge.draft) are a
    real, tested channel - assistant.draft_write - not just a reserved name. Same shape as
    test_the_b2_ingestion_channel_is_registered_and_actually_exercised above: the channel is
    declared, at least one registered case names it, that case is one of the outcomes run_all
    actually produced, and it passes."""
    assert "assistant.draft_write" in INJECTION_CHANNELS

    channel_cases = {c.name for c in CASES if c.channel == "assistant.draft_write"}
    assert channel_cases, "assistant.draft_write is declared but no registered case names it"

    executed = {o.case.name: o for o in outcomes if o.case.name in channel_cases}
    assert executed.keys() == channel_cases, (
        f"case(s) for assistant.draft_write registered but never executed by run_all: "
        f"{sorted(channel_cases - executed.keys())}"
    )

    failures = [o for o in executed.values() if not o.passed]
    assert not failures, "assistant.draft_write case(s) failed:\n" + format_report(failures)


# --------------------------------------------------------------------------- 2: the properties


def test_non_hole_cases_all_pass(outcomes):
    failures = [o for o in outcomes if not o.passed and o.case.name not in KNOWN_HOLES]
    assert not failures, "unexpected eval failure(s):\n" + format_report(failures)


def test_claim_status_forgery_is_now_caught(outcomes):
    """Was the strict-xfail-pinned known hole; fixed in nvplan/ai/assistant.py
    (``_check_claim_segment`` now compares a ``ClaimSegment``'s status AND title against the
    real ``Claim`` row - the same shape as ``_check_figure``'s value_mismatch - and raises
    ``claim_status_mismatch``/``claim_title_mismatch`` on disagreement), so a hostile
    instruction asking the assistant to report a 'pending' decision as 'decided' is now
    rejected rather than persisted.

    Note (follow-up, outside this file's ownership): ``nvplan.ai.evals.cases.KNOWN_HOLES``
    still lists ``assistant.claim_status_forgery_not_flagged`` as a known hole, and the case's
    own docstring/name still describe it that way. Since ``cases.py`` belongs to a different
    work package, this test only re-pins the underlying property directly rather than editing
    that file; ``KNOWN_HOLES`` should have this entry removed once that package picks it up
    (``test_non_hole_cases_all_pass`` does not require that - it only skips cases that ARE in
    KNOWN_HOLES, so it stays green either way in the meantime)."""
    (outcome,) = [o for o in outcomes if o.case.name == "assistant.claim_status_forgery_not_flagged"]
    assert outcome.passed, outcome.error or outcome.observation
