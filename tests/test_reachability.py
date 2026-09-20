"""Reachability for the AI layer: a tool or skill that is built, tested and even committed but
that no agent actually loads must fail this suite rather than ship silently.

The miss this guards against (see the git history around "Close three scanner escapes; give the
assistant its own touchpoint" and the strategy-skills commits that followed it): six strategy
skills were built and unit-tested while the assistant - the only surface with any use for them -
passed no skills source at all. tests/test_ai_skills.py checks that skill files exist and parse;
it never checked that anything could load them. This file is the missing check, in two shapes:

1. Tool reachability (both directions) between what the two factories in nvplan/ai/tools.py
   produce and what the three touchpoints plus the assistant actually consume.
2. Skill reachability: which agents pass a skills source at all (determined by inspecting the
   builder's own source, not by assuming "skills=" is there because it looks like it should be),
   and whether that matches an explicit, asserted-as-data mapping of which skill is FOR which
   surface (nvplan.ai.context.SKILL_SURFACE - imported here as INTENDED_SKILL_SURFACE rather
   than duplicated, so this file asserts against the real mapping instead of a hand-kept copy
   that could drift from it). The gap this file originally caught - the assistant passing no
   skills source at all - is now closed (build_assistant_agent passes
   skills=[SKILLS_SOURCE], scoped to its own skills via its own private backend - see
   nvplan/ai/assistant.py and nvplan/ai/context.py:make_backend); fixing that half without
   scoping the touchpoints would have left every touchpoint still carrying the other nine
   skills' index entries as noise, so build_touchpoint_agent/touchpoint_subagents were scoped
   too (nvplan/ai/context.py:skills_for_surface / skills_index_route /
   make_shared_touchpoint_backend).

Nothing here builds a real model or calls the Anthropic API: skill reachability is decided by
static inspection of builder source (`inspect.getsource`), and tool reachability by constructing
the `@tool`-wrapped functions (which run no query until invoked - construction only, over the
same seeded sqlite fixture tests/test_ai_guardrails.py uses)."""

from __future__ import annotations

import inspect
import re

import pytest
from langchain_core.tools import BaseTool

from nvplan.ai.agents import _TOUCHPOINT_TOOLS, build_advisor, build_touchpoint_agent, touchpoint_subagents
from nvplan.ai.assistant import ASSISTANT_READ_TOOLS, ASSISTANT_WRITE_TOOLS, assistant_tools, build_assistant_agent
from nvplan.ai.context import SKILLS_DIR, SKILL_NAMES, SKILL_SURFACE
from nvplan.ai.tools import AiRunContext, make_read_tools, make_write_tools, record_proposal
from test_ai_fixtures import ai_db  # noqa: F401 (fixture)

# --------------------------------------------------------------------------- shared name-sets


def _produced_tool_names(factory) -> set[str]:
    """Every tool name either factory in nvplan/ai/tools.py can hand to an agent."""
    read_tools = make_read_tools(factory)
    write_tools = make_write_tools(factory, AiRunContext())
    return {t.name for t in [*read_tools, *write_tools]}


def _touchpoint_consumer_names() -> set[str]:
    """Union of every name any touchpoint's read or write set names, straight from
    nvplan/ai/agents.py:_TOUCHPOINT_TOOLS - the raw declared names, not the filtered output of
    touchpoint_tools(), because the filter (`t.name in wanted`) silently drops a typo instead of
    raising one; comparing the declared names against the produced universe is what catches it."""
    names: set[str] = set()
    for read_names, write_names in _TOUCHPOINT_TOOLS.values():
        names |= read_names | write_names
    return names


def _assistant_consumer_names() -> set[str]:
    return set(ASSISTANT_READ_TOOLS) | set(ASSISTANT_WRITE_TOOLS)


def _all_consumer_names() -> set[str]:
    return _touchpoint_consumer_names() | _assistant_consumer_names()


# --------------------------------------------------------------------------- 1: record_proposal is not a tool


def test_record_proposal_is_a_plain_validator_not_a_registered_tool(ai_db):
    """record_proposal (nvplan/ai/tools.py) is called BY record_revenue_proposal - it takes a
    live Session and an AiRunContext, neither of which an agent can produce as tool arguments, so
    it must never itself appear as a tool an agent could call."""
    factory, _ = ai_db
    assert not isinstance(record_proposal, BaseTool)
    assert "record_proposal" not in _produced_tool_names(factory)


# --------------------------------------------------------------------------- 2: every produced tool is reachable


def test_every_produced_tool_is_reachable_by_some_consumer(ai_db):
    """A tool make_read_tools/make_write_tools builds but that no touchpoint's set and neither
    of ASSISTANT_READ_TOOLS/ASSISTANT_WRITE_TOOLS names is built and tested in isolation but
    never reachable from any agent - built but never wired, the exact shape of the miss this
    file exists to catch."""
    factory, _ = ai_db
    orphans = _produced_tool_names(factory) - _all_consumer_names()
    assert not orphans, (
        f"tool(s) {sorted(orphans)} are produced by make_read_tools/make_write_tools "
        f"(nvplan/ai/tools.py) but named by no consumer - wire each one into whichever surface "
        f"should call it: add it to the right touchpoint's set in "
        f"nvplan/ai/agents.py:_TOUCHPOINT_TOOLS, or to ASSISTANT_READ_TOOLS/ASSISTANT_WRITE_TOOLS "
        f"in nvplan/ai/assistant.py"
    )


# --------------------------------------------------------------------------- 3: every named tool exists


def test_every_consumer_named_tool_actually_exists(ai_db):
    """The reverse miss: a name listed in a touchpoint's tool set, or in
    ASSISTANT_READ_TOOLS/ASSISTANT_WRITE_TOOLS, that no factory produces is a typo silently
    dropping a capability - touchpoint_tools()/assistant_tools() filter with
    `t.name in wanted_names`, so a misspelled wanted name just produces one fewer tool, with no
    error anywhere."""
    factory, _ = ai_db
    produced = _produced_tool_names(factory)
    typos = _all_consumer_names() - produced
    assert not typos, (
        f"name(s) {sorted(typos)} are named by a touchpoint's tool set (nvplan/ai/agents.py:"
        f"_TOUCHPOINT_TOOLS) or by ASSISTANT_READ_TOOLS/ASSISTANT_WRITE_TOOLS "
        f"(nvplan/ai/assistant.py) but no tool of that name exists in make_read_tools/"
        f"make_write_tools (nvplan/ai/tools.py) - almost certainly a typo where the name is "
        f"declared; fix it there rather than adding a new tool"
    )


def test_assistant_tools_actually_built_match_the_declared_sets(ai_db):
    """Sanity cross-check that assistant_tools() (the real production filter) produces exactly
    what ASSISTANT_READ_TOOLS/ASSISTANT_WRITE_TOOLS declare, once both sets are known-valid
    (previous test) - i.e. the declared sets are not merely a stale comment next to the code
    that actually decides the assistant's tools."""
    factory, _ = ai_db
    ctx = AiRunContext()
    built = {t.name for t in assistant_tools(factory, ctx)}
    assert built == _assistant_consumer_names()


# --------------------------------------------------------------------------- 4: skills - who loads them at all


def _passes_skills_source(func) -> bool:
    """True when func's OWN source hands a skills SOURCE to create_deep_agent - as the
    `skills=` kwarg (the standalone builders' unchanged `skills=[SKILLS_SOURCE]`, pruned per
    surface by the private backend each of them now builds - see nvplan/ai/context.py's
    make_backend(surface=...)), or as one subagent spec's own `"skills"` key (the shared-backend
    case, where the source itself has to be the thing that varies per subagent - see
    nvplan/ai/context.py's skills_index_route / make_shared_touchpoint_backend, and
    touchpoint_subagents' own docstring for why). Inspected rather than assumed: the point of
    this file is that a call site can look wired (it builds, it has a `backend=`, a
    `system_prompt=`, tools, everything else an agent needs) while simply never mentioning
    skills at all, and nothing about its shape says so short of reading it."""
    src = inspect.getsource(func)
    return bool(re.search(r"skills\s*=\s*\[\s*SKILLS_SOURCE\s*\]", src)) or bool(
        re.search(r'"skills"\s*:\s*\[\s*skills_index_route\(', src)
    )


def test_every_skill_owning_builder_passes_a_scoped_skills_source():
    """build_touchpoint_agent, touchpoint_subagents and build_assistant_agent each pass a
    skills source scoped to their own surface's skills, never the whole directory undifferentiated
    (nvplan/ai/context.py: make_backend(surface=...) for the two standalone builders,
    skills_index_route(...) per subagent for touchpoint_subagents - see _passes_skills_source's
    own docstring for which mechanism each uses and why they differ). Confirmed by inspection,
    not assumed.

    build_advisor itself passes none of its own: nvplan.ai.context.SKILL_SURFACE maps no skill
    to an "advisor" surface - it only orchestrates the three touchpoints (each of which reads
    its own skill as a subagent, already covered above) and does no strategy analysis or
    citation-checked answering itself, so it has nothing of its own to scope."""
    assert _passes_skills_source(build_touchpoint_agent)
    assert _passes_skills_source(touchpoint_subagents)
    assert _passes_skills_source(build_assistant_agent)
    assert not _passes_skills_source(build_advisor)


def test_every_skill_on_disk_is_reachable_by_at_least_one_agent():
    """Every directory under SKILLS_DIR must be readable by SOME agent-building call site.

    Reachability is no longer "true for all of them because one builder loads the whole
    directory" (that was the noise this suite's fix removed) - it is computed from the real
    mapping (SKILL_SURFACE) and whether the surface that owns each skill has a builder that
    passes a skills source at all (previous test). A skill dir that no builder's source
    mentioned at all - directly, or via its surface's scoped source - would be truly dead code
    with no path to any agent."""
    on_disk = {p.name for p in SKILLS_DIR.iterdir() if p.is_dir()}
    reachable = {skill for skill, surface in SKILL_SURFACE.items() if _passes_skills_source(_SURFACE_BUILDER[surface])}
    orphans = on_disk - reachable
    assert not orphans, (
        f"skill dir(s) {sorted(orphans)} exist under {SKILLS_DIR} but no agent-building call "
        f"site passes a skills source that would reach them - add its surface to "
        f"nvplan/ai/context.py:SKILL_SURFACE and make sure that surface's builder passes one"
    )


# tests/test_ai_skills.py::test_skill_files_have_valid_frontmatter already asserts
# {p.name for p in SKILLS_DIR.iterdir() if p.is_dir()} == set(SKILL_NAMES) (manifest matches
# disk, both directions) - not duplicated here.


# --------------------------------------------------------------------------- 5: relevance, as data (no more xfail)

#: Which surface each skill is FOR - the real mapping (nvplan.ai.context.SKILL_SURFACE), asserted
#: against directly rather than duplicated as a second, driftable copy: adding a skill on disk
#: without adding a line to SKILL_SURFACE itself now fails test_every_skill_has_an_intended_surface
#: below, the same as it would have against a hand-kept copy - but there is only one place left to
#: update, and every skills= consumer (nvplan/ai/context.py's make_backend /
#: make_shared_touchpoint_backend) already reads that same dict.
INTENDED_SKILL_SURFACE: dict[str, str] = SKILL_SURFACE

#: The builder that constructs the named surface's agent, for the skills-source check.
_SURFACE_BUILDER = {
    "env_scan": build_touchpoint_agent,
    "revenue_proposal": build_touchpoint_agent,
    "deviation_explanation": build_touchpoint_agent,
    "assistant": build_assistant_agent,
}


def test_every_skill_has_an_intended_surface():
    """INTENDED_SKILL_SURFACE must name exactly the skills on disk, no more, no fewer, and every
    surface it names must be one _SURFACE_BUILDER knows how to build - so the mapping can never
    silently go stale against either side."""
    assert set(INTENDED_SKILL_SURFACE) == set(SKILL_NAMES)
    assert set(INTENDED_SKILL_SURFACE.values()) == set(_SURFACE_BUILDER)


def test_touchpoint_owned_skills_are_reachable_by_their_intended_surface():
    """The three touchpoint-owned skills (env-scan-54-positions, revenue-proposal-method,
    deviation-explanation-method) ARE reachable by the surface INTENDED_SKILL_SURFACE says owns
    them, because build_touchpoint_agent passes a skills source scoped to that very surface. This
    is the half of the mapping that already held before the fix; the assistant half (once
    xfailing, now fixed) is the next test."""
    touchpoint_owned = {s: surf for s, surf in INTENDED_SKILL_SURFACE.items() if surf != "assistant"}
    assert touchpoint_owned, "expected at least one touchpoint-owned skill to check against"
    for skill, surface in touchpoint_owned.items():
        builder = _SURFACE_BUILDER[surface]
        assert _passes_skills_source(builder), (
            f"{skill!r} is mapped to surface {surface!r} in INTENDED_SKILL_SURFACE but "
            f"{builder.__name__} passes no skills source at all - it would be unreachable"
        )


def test_assistant_owned_skills_are_reachable_by_the_assistant():
    """The miss this file exists to catch, now fixed: build_assistant_agent
    (nvplan/ai/assistant.py) passes skills=[SKILLS_SOURCE] to create_deep_agent, backed by its
    own private backend pruned to just the assistant's skills (nvplan/ai/context.py:
    make_backend(surface="assistant")), so the six strategy-* skills and
    assistant-citation-method - mapped to the assistant surface in INTENDED_SKILL_SURFACE
    because it is, per this module's own framing, the only surface built to invoke them - are
    now reachable by the one agent that owns them. This was the xfail this test used to carry;
    it now passes outright."""
    assistant_owned = {s: surf for s, surf in INTENDED_SKILL_SURFACE.items() if surf == "assistant"}
    assert assistant_owned, "expected at least one assistant-owned skill to check against"
    unreachable = [
        skill for skill, surface in assistant_owned.items() if not _passes_skills_source(_SURFACE_BUILDER[surface])
    ]
    assert not unreachable, (
        f"skill(s) {sorted(unreachable)} are mapped to the assistant surface in "
        f"INTENDED_SKILL_SURFACE but build_assistant_agent passes no skills source - built and "
        f"unit-tested (nvplan/ai/skills/*/SKILL.md) but unreachable by the agent that owns them; "
        f"add skills=[SKILLS_SOURCE] to build_assistant_agent's create_deep_agent(...) call in "
        f"nvplan/ai/assistant.py"
    )


# --------------------------------------------------------------------------- 6: A1 - the keyless default is real
#
# The miss this guards against: a provider seam that LOOKS wired (resolve_model exists, is
# documented, is unit-tested in isolation) while every real entrypoint still calls get_model
# directly - which raises MissingCredentials with no key, exactly as before A1, and the "offline
# by default" claim would be false for anyone who doesn't happen to read the diff. Checked two
# ways, the same shape test_reachability.py already uses for skills: (1) the object actually
# constructed under default configuration with no credential, and (2) static inspection of each
# entrypoint's own source, so a future edit that quietly reverts one call site to get_model(model)
# fails here even though get_model() itself still works (by design - see its own docstring).


def test_default_resolution_takes_the_deterministic_path_with_no_credential(monkeypatch):
    from nvplan.ai import agents
    from nvplan.ai.fake import DeterministicChatModel

    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(agents, "_ant_cli", lambda: None)
    monkeypatch.setattr(agents.config, "AI_PROVIDER", "auto", raising=False)

    assert agents.credentials_available() is False
    model = agents.resolve_model(None)
    assert isinstance(model, DeterministicChatModel), (
        "resolve_model(None) built something other than the deterministic provider with no "
        "credential and config.AI_PROVIDER == 'auto' - the AI layer would silently require a "
        "key again"
    )


def test_default_resolution_stays_live_when_a_credential_is_present(monkeypatch):
    """The other half: a credential must still drive the real client under "auto" - A1 must not
    make the deterministic provider win unconditionally."""
    from nvplan.ai import agents

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-reachability-dummy")
    monkeypatch.setattr(agents.config, "AI_PROVIDER", "auto", raising=False)
    model = agents.resolve_model(None)
    assert type(model).__name__ == "ChatAnthropic"


def test_provider_override_forces_deterministic_even_with_a_credential(monkeypatch):
    from nvplan.ai import agents
    from nvplan.ai.fake import DeterministicChatModel

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-reachability-dummy")
    monkeypatch.setattr(agents.config, "AI_PROVIDER", "deterministic", raising=False)
    assert isinstance(agents.resolve_model(None), DeterministicChatModel)


def test_provider_override_forces_live_and_still_raises_without_a_credential(monkeypatch):
    """"live" is a deliberate override, never a silent fallback: with no key it must still raise
    MissingCredentials exactly as get_model() does, not quietly hand back the deterministic
    provider."""
    from nvplan.ai import agents

    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(agents, "_ant_cli", lambda: None)
    monkeypatch.setattr(agents.config, "AI_PROVIDER", "live", raising=False)
    with pytest.raises(agents.MissingCredentials):
        agents.resolve_model(None)


def test_every_touchpoint_entrypoint_resolves_its_own_default_through_resolve_model():
    """Static check on the real source of each entrypoint that owns a model=None default: it
    must call resolve_model for that default, not get_model directly (see resolve_model's own
    docstring for why the two are not interchangeable).

    run_env_scan and assistant.ask are both checked separately: each is an entrypoint that can
    write a brain/ file (run_env_scan since B2, assistant.ask since B3's draft_decision/
    draft_hypotheses), so each must pass ``provider=config.AI_BRAIN_WRITE_PROVIDER`` rather than
    falling through to the shared ``config.AI_PROVIDER`` default the other two touchpoints use -
    see resolve_model's own docstring for why "auto" is wrong for a path that writes into the
    source of truth."""
    import inspect

    from nvplan.ai import agents, assistant

    for fn in (agents.run_revenue_proposal, agents.run_deviation_explanation):
        src = inspect.getsource(fn)
        assert "resolve_model(model)" in src, (
            f"{fn.__name__} (nvplan/ai/agents.py) does not call resolve_model(model) for its own "
            f"default - it would need a credential again with no explicit model= passed in"
        )
    env_scan_src = inspect.getsource(agents.run_env_scan)
    assert "resolve_model(model, provider=config.AI_BRAIN_WRITE_PROVIDER)" in env_scan_src, (
        "run_env_scan (nvplan/ai/agents.py) does not resolve its default through "
        "config.AI_BRAIN_WRITE_PROVIDER (B2) - it would silently go live under config.AI_PROVIDER "
        "== 'auto' whenever a credential is present, exactly the case this knob exists to avoid "
        "for the one entrypoint that can write into brain/"
    )
    assistant_src = inspect.getsource(assistant.ask)
    assert "resolve_model(model, provider=config.AI_BRAIN_WRITE_PROVIDER)" in assistant_src, (
        "nvplan.ai.assistant.ask (B3) does not resolve its default through "
        "config.AI_BRAIN_WRITE_PROVIDER - every conversation can now end in a brain write via "
        "draft_decision/draft_hypotheses, so it must not go live just because a credential is "
        "sitting in .env, exactly the case this knob exists to avoid"
    )


# --------------------------------------------------------------------------- 7: B2 - the brain writer is reachable
#
# The miss this guards against: brainkit/writer.py (B1) shipped fully built and unit-tested with
# zero callers outside its own tests (tests/test_brainkit_writer.py) - a write substrate nobody
# calls is exactly as dead as a tool no agent loads or a skill no builder passes. B2 gives it its
# first real caller (bridge/ingest.py, called by nvplan.ai.agents.run_env_scan); this test pins
# that fact statically so a future refactor can't quietly strand the writer again without this
# suite catching it - the same "inspect the real source, don't assume the shape is right because
# it looks wired" discipline test 4/6 above already use for skills and the provider seam.


def test_brainkit_writer_has_a_real_non_test_caller():
    """brainkit.writer.draft_ingestion/draft_decision/draft_hypotheses must be called from at
    least one non-test module - today that is bridge/ingest.py (draft_ingestion, from
    nvplan.ai.agents.run_env_scan). Grepping source (not just checking an import line) so a
    module that imports brainkit.writer but never actually calls one of its draft_* functions
    still fails this - importing is not calling."""
    import inspect

    from brainkit import writer as brainkit_writer
    from bridge import ingest as bridge_ingest

    draft_fn_names = [name for name in brainkit_writer.__all__ if name.startswith("draft_")]
    assert draft_fn_names, "brainkit.writer.__all__ names no draft_* function to check reachability for"

    ingest_src = inspect.getsource(bridge_ingest)
    called = [name for name in draft_fn_names if f"{name}(" in ingest_src]
    assert called, (
        f"none of {draft_fn_names} is called from bridge/ingest.py - brainkit.writer would be "
        f"built, unit-tested and orphaned again exactly as it was before B2"
    )

    agents_src = inspect.getsource(__import__("nvplan.ai.agents", fromlist=["run_env_scan"]).run_env_scan)
    assert "write_env_scan_ingestion(" in agents_src, (
        "nvplan.ai.agents.run_env_scan no longer calls bridge.ingest.write_env_scan_ingestion - "
        "the brain-writing path (B2) would be built but never actually invoked by the touchpoint "
        "that is supposed to use it"
    )


# --------------------------------------------------------------------------- 8: B3 - drafting is reachable, and only by the assistant
#
# The same miss test 7 guards against, applied to draft_decision/draft_hypotheses (PLATFORM.md
# §12.6): brainkit.writer's own draft_decision/draft_hypotheses functions could ship fully built
# and unit-tested with bridge/draft.py as their only caller and nvplan.ai.tools never actually
# calling bridge.draft - a write substrate with a caller that itself has no caller is exactly as
# dead as one with none. Checked both ways: reachability (built but unreachable) and containment
# (reachable from a surface that was never supposed to carry it).


def test_brainkit_draft_decision_and_hypotheses_have_a_real_non_test_caller():
    """draft_decision/draft_hypotheses must be called from bridge/draft.py (B3's own caller,
    mirroring bridge/ingest.py for draft_ingestion - test 7 above), and bridge.draft's own
    draft_decision_and_index/draft_hypotheses_and_index must in turn be called from
    nvplan/ai/tools.py's make_write_tools - the only place anything in nvplan.ai is allowed to
    reach them, since nvplan.ai must never import brainkit directly (PLATFORM.md §7.1)."""
    import inspect

    from brainkit import writer as brainkit_writer
    from bridge import draft as bridge_draft

    draft_fn_names = [name for name in brainkit_writer.__all__ if name.startswith("draft_")]
    bridge_draft_src = inspect.getsource(bridge_draft)
    called = [name for name in draft_fn_names if f"{name}(" in bridge_draft_src]
    assert set(called) >= {"draft_decision", "draft_hypotheses"}, (
        f"bridge/draft.py does not call both draft_decision and draft_hypotheses (found: "
        f"{sorted(called)}) - brainkit.writer's decision/hypothesis drafting would be built, "
        f"unit-tested and orphaned, exactly the miss B2's own reachability test (7 above) guards "
        f"against for draft_ingestion"
    )

    tools_src = inspect.getsource(__import__("nvplan.ai.tools", fromlist=["make_write_tools"]))
    assert "draft_decision_and_index(" in tools_src and "draft_hypotheses_and_index(" in tools_src, (
        "nvplan/ai/tools.py does not call bridge.draft.draft_decision_and_index/"
        "draft_hypotheses_and_index - bridge/draft.py would be built and tested with no caller "
        "in the AI layer at all"
    )


def test_draft_decision_and_hypotheses_are_named_only_by_the_assistant():
    """The containment half: draft_decision/draft_hypotheses must be reachable from
    ASSISTANT_WRITE_TOOLS and named by no touchpoint's _TOUCHPOINT_TOOLS write set - a touchpoint
    silently acquiring a brain-write capability nothing asked it to carry would be exactly the
    kind of unnoticed capability tests/test_ai_guardrails.py's explicit-enumeration discipline
    exists to catch, applied here to reachability instead of to the tool-name allow-list itself."""
    draft_tool_names = {"draft_decision", "draft_hypotheses"}
    assert draft_tool_names <= set(ASSISTANT_WRITE_TOOLS)
    for touchpoint, (_read_names, write_names) in _TOUCHPOINT_TOOLS.items():
        overlap = draft_tool_names & write_names
        assert not overlap, f"touchpoint {touchpoint!r} carries {sorted(overlap)} - only the assistant should"
