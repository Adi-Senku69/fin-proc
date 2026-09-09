"""Context-window management + per-call audit, offline with the scripted fake model.

Covers: compiled middleware order, summarization with history offload (state only, never
disk/DB), tool-result eviction to /large_tool_results/, ClearToolUsesEdit with the write
tools excluded, the persisted call log, the API detail route, and the config defaults.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from nvplan import config
from nvplan.ai import ContextPolicy, DEFAULT_POLICY, build_touchpoint_agent, run_deviation_explanation, run_revenue_proposal
from nvplan.ai.audit import CALL_LOG_KEYS, CLEARED_PLACEHOLDER, EVICTED_MARKER
from nvplan.ai.context import CLEAR_EXCLUDED_TOOLS, SKILLS_DIR, build_context_middleware, make_backend, middleware_names
from nvplan.ai.fake import (
    FakeToolCallingModel,
    ai_calls,
    fake_summary_model,
    read_skill,
    scripted_deviation_model,
    scripted_revenue_model,
    structured,
    tool_call,
)
from nvplan.ai.schemas import DeviationExplanation, EnvScanResult
from nvplan.ai.tools import AiRunContext
from nvplan.api.app import create_app
from nvplan.db.models import AiRecord, Base
from test_ai_fixtures import PLAN_YEAR, ai_db  # noqa: F401 (fixture)

AI_PKG_DIR = SKILLS_DIR.parent
HUGE = 10**9


def _repo_files() -> list[str]:
    return sorted(str(p) for p in AI_PKG_DIR.rglob("*") if "__pycache__" not in p.parts)


def _row_counts(factory) -> dict[str, int]:
    with factory() as s:
        return {t.name: s.scalar(select(func.count()).select_from(t)) for t in Base.metadata.sorted_tables}


def _deviation_script(n_tool_turns: int, tool: str = "get_actuals") -> FakeToolCallingModel:
    expl = DeviationExplanation(scenario="base", year=PLAN_YEAR, summary="scripted", contributions=[])
    turns = [read_skill("deviation_explanation")]
    turns += [ai_calls(tool_call(tool, {}, f"t{i}")) for i in range(n_tool_turns)]
    turns.append(structured("DeviationExplanation", expl.model_dump(), content="done"))
    return FakeToolCallingModel(responses=turns)


def _note_id(factory) -> int:
    from nvplan.db.models import ExternalNote

    with factory() as s:
        return s.scalar(select(ExternalNote.id).order_by(ExternalNote.id))


# --------------------------------------------------------------------------- order


def test_middleware_order_audit_after_context(ai_db):
    factory, _ = ai_db
    agent = build_touchpoint_agent(
        "revenue_proposal", model=FakeToolCallingModel(responses=[]), session_factory=factory, extra_context=AiRunContext()
    )
    order = middleware_names(agent)
    assert order == [
        "SkillsMiddleware",
        "FilesystemMiddleware",
        "SubAgentMiddleware",
        "SummarizationMiddleware",
        "ContextEditingMiddleware",
        "ContextAuditMiddleware",
        "AnthropicPromptCachingMiddleware",
    ]
    # the audit is inside summarization and clearing, so it sees the rewritten request
    assert order.index("ContextAuditMiddleware") > order.index("SummarizationMiddleware")
    assert order.index("ContextAuditMiddleware") > order.index("ContextEditingMiddleware")


# --------------------------------------------------------------------------- summarization


def test_summarization_fires_and_offloads_to_state_only(ai_db):
    factory, _ = ai_db
    files_before = _repo_files()
    rows_before = _row_counts(factory)
    ctx = AiRunContext()
    policy = ContextPolicy(
        summarization_trigger_tokens=6_000,
        summarization_keep_messages=2,
        clear_tool_uses_trigger_tokens=HUGE,
        tool_result_evict_tokens=HUGE,
        summarization_model=fake_summary_model("FAKE SUMMARY OF THE EARLIER TOOL READS"),
    )
    agent = build_touchpoint_agent(
        "deviation_explanation", model=_deviation_script(15), session_factory=factory, extra_context=ctx, policy=policy
    )
    result = agent.invoke({"messages": [{"role": "user", "content": "explain"}]})
    assert result["structured_response"].summary == "scripted"

    log = ctx.call_log
    assert len(log) == 17  # skill read + 15 tool turns + structured
    flags = [c["summarized"] for c in log]
    assert flags[0] is False and any(flags)
    first = flags.index(True)
    assert first > 1, "summarization should fire mid-run, not on the first calls"
    assert log[first]["approx_tokens"] < log[first - 1]["approx_tokens"]
    assert log[first]["n_messages"] <= policy.summarization_keep_messages + 1
    assert all(flags[first:]), "once summarized, every later request carries the summary"
    first_msg = log[first]["request"]["messages"][0]
    assert first_msg["role"] == "human" and first_msg.get("summary") is True
    assert "FAKE SUMMARY OF THE EARLIER TOOL READS" in first_msg["content_excerpt"]
    assert "/conversation_history/" in first_msg["content_excerpt"]

    # the offloaded history lives in graph state, not on disk and not in the DB
    history = [p for p in result["files"] if p.startswith("/conversation_history/")]
    assert len(history) == 1 and history[0].endswith(".md")
    content = result["files"][history[0]]["content"]
    text = content if isinstance(content, str) else "\n".join(content)
    assert "get_actuals" in text and "Summarized at" in text
    assert _repo_files() == files_before
    assert _row_counts(factory) == rows_before  # the agent alone writes nothing
    # state messages are untouched by design (summarization rewrites the request only)
    assert sum(1 for m in result["messages"] if m.type == "tool") == 17


def test_summarization_via_entrypoint_persists_call_log_only(ai_db):
    factory, _ = ai_db
    rows_before = _row_counts(factory)
    policy = ContextPolicy(
        summarization_trigger_tokens=6_000,
        summarization_keep_messages=2,
        clear_tool_uses_trigger_tokens=HUGE,
        tool_result_evict_tokens=HUGE,
        summarization_model=fake_summary_model(),
    )
    rec = run_deviation_explanation(factory, scenario_kind="base", year=PLAN_YEAR, model=_deviation_script(15), policy=policy)
    after = _row_counts(factory)
    assert after == {**rows_before, "ai_record": rows_before["ai_record"] + 1}
    with factory() as s:
        log = s.get(AiRecord, rec.id).call_log_json
    assert any(c["summarized"] for c in log)


# --------------------------------------------------------------------------- eviction


def test_large_tool_result_is_evicted_and_readable(ai_db):
    factory, _ = ai_db
    ctx = AiRunContext()
    scan = EnvScanResult(flagged=[], summary="nothing material")
    model = FakeToolCallingModel(
        responses=[
            read_skill("env_scan"),
            ai_calls(tool_call("get_env_framework", {}, "fw")),
            ai_calls(tool_call("read_file", {"file_path": "/large_tool_results/fw", "offset": 0, "limit": 40}, "rd")),
            structured("EnvScanResult", scan.model_dump(), content=scan.summary),
        ]
    )
    # 300 tokens = 1200 chars: the pretty-printed framework (~6k chars) is evicted, the skill
    # (read_file, excluded) is not, and a 40-line page of the offloaded file still fits.
    policy = ContextPolicy(tool_result_evict_tokens=300, clear_tool_uses_trigger_tokens=HUGE, summarization_trigger_tokens=HUGE)
    agent = build_touchpoint_agent("env_scan", model=model, session_factory=factory, extra_context=ctx, policy=policy)
    result = agent.invoke({"messages": [{"role": "user", "content": "scan"}]})

    tools = {m.tool_call_id: m for m in result["messages"] if m.type == "tool"}
    evicted = tools["fw"]
    assert EVICTED_MARKER in evicted.content and "/large_tool_results/fw" in evicted.content
    assert "D6" not in evicted.content.split("[")[0]  # the pointer carries a preview, not the payload
    assert "/large_tool_results/fw" in result["files"]
    # the skill read is never evicted (filesystem tools are excluded), even at 50 tokens
    assert EVICTED_MARKER not in tools["skill-env_scan"].content
    # the scripted read_file on the pointer returns the framework text
    assert "Government stability" in tools["rd"].content and "D1.P1" in tools["rd"].content
    assert "ILLUSTRATIVE" in tools["rd"].content

    # audit saw the pointer, not the payload
    evicted_calls = [c for c in ctx.call_log if c["evicted_tool_results"]]
    assert evicted_calls and evicted_calls[0]["call_index"] == 2
    entry = next(m for m in evicted_calls[0]["request"]["messages"] if m.get("tool_call_id") == "fw")
    assert entry["evicted"] is True


# --------------------------------------------------------------------------- clearing


def test_clear_tool_uses_keeps_last_and_excludes_write_tools(ai_db):
    factory, _ = ai_db
    ctx = AiRunContext(model_version="fake")
    scan = EnvScanResult(flagged=[], summary="one note")
    note = tool_call(
        "record_external_note",
        {"text": "Wage rounds +4% p.a. 2026-2027 (assumption/illustrative)", "domain": "D2 Economic", "position": "D2.P4 Wage growth", "category_code": "PERS", "year": 2026},
        "note0",
    )
    turns = [read_skill("env_scan"), ai_calls(note)]
    turns += [ai_calls(tool_call("get_parameters", {}, f"p{i}")) for i in range(6)]
    turns.append(structured("EnvScanResult", scan.model_dump(), content=scan.summary))
    policy = ContextPolicy(
        clear_tool_uses_trigger_tokens=800, clear_tool_uses_keep=2, summarization_trigger_tokens=HUGE, tool_result_evict_tokens=HUGE
    )
    agent = build_touchpoint_agent(
        "env_scan", model=FakeToolCallingModel(responses=turns), session_factory=factory, extra_context=ctx, policy=policy
    )
    result = agent.invoke({"messages": [{"role": "user", "content": "scan"}]})
    assert result["structured_response"].summary == "one note"

    last = ctx.call_log[-1]
    assert last["cleared_tool_results"] >= 1
    tool_entries = [m for m in last["request"]["messages"] if m["role"] == "tool"]
    kept = tool_entries[-policy.clear_tool_uses_keep :]
    assert all(not e.get("cleared") for e in kept)
    older = tool_entries[: -policy.clear_tool_uses_keep]
    for e in older:
        if e["tool_name"] in CLEAR_EXCLUDED_TOOLS:
            assert not e.get("cleared") and "note_id" in e["content_excerpt"]
        else:
            assert e.get("cleared") is True and e["content_excerpt"] == CLEARED_PLACEHOLDER
    assert any(e["tool_name"] == "record_external_note" for e in older)
    # state is untouched: clearing is request-only
    assert all(CLEARED_PLACEHOLDER not in m.content for m in result["messages"] if m.type == "tool")


# --------------------------------------------------------------------------- call log persistence + API


def test_call_log_persisted_with_documented_keys(ai_db):
    factory, _ = ai_db
    model = scripted_revenue_model(year=2027, proposed_value=21_000.0, rationale="contract ends Q2 2027", cited_note_ids=[_note_id(factory)])
    rec = run_revenue_proposal(factory, scenario_kind="base", year=2027, default_value=22_000.0, model=model)
    with factory() as s:
        log = s.get(AiRecord, rec.id).call_log_json
    assert isinstance(log, list) and len(log) == 4  # skill read, 3 reads, record, structured
    for i, call in enumerate(log):
        assert set(CALL_LOG_KEYS) <= set(call)
        assert call["call_index"] == i and call["n_messages"] >= 1 and call["approx_tokens"] > 0
        assert "Skills System" in call["request"]["system"] and call["system_prompt_chars"] == len(call["request"]["system"])
        assert "record_revenue_proposal" in call["tools_offered"] and "write_file" not in call["tools_offered"]
        assert call["summarized"] is False and call["cleared_tool_results"] == 0 and call["evicted_tool_results"] == 0
    assert log[0]["response"]["tool_calls"] == ["read_file"]
    assert log[2]["response"]["tool_calls"] == ["record_revenue_proposal"]
    assert log[-1]["response"]["tool_calls"] == ["RevenueProposal"]
    assert log[-1]["request"]["messages"][0]["role"] == "human" and "22,000.0" in log[-1]["request"]["messages"][0]["content_excerpt"]
    # the existing prompt_text behaviour is unchanged: system + ---USER--- + rendered prompt
    with factory() as s:
        r = s.get(AiRecord, rec.id)
        assert r.prompt_text.startswith("---SYSTEM---") and "---USER---" in r.prompt_text


def test_api_detail_route_exposes_call_log_and_list_stays_light(ai_db, monkeypatch):
    factory, _ = ai_db
    app = create_app(session_factory=factory)
    app.state.model_factory = lambda tp, ctx: scripted_deviation_model(scenario_kind="base", year=PLAN_YEAR, summary="api", contributions=[])
    with TestClient(app) as client:
        r = client.post("/ai/deviation-explanation", json={"scenario_kind": "base", "year": PLAN_YEAR})
        assert r.status_code == 200, r.text
        rec_id = r.json()["id"]
        assert "call_log" not in r.json()
        detail = client.get(f"/ai/records/{rec_id}").json()
        assert isinstance(detail["call_log"], list) and len(detail["call_log"]) == 3  # skill read, pva, structured
        assert set(CALL_LOG_KEYS) <= set(detail["call_log"][0])
        assert detail["prompt_text"] == r.json()["prompt_text"]
        listed = client.get("/ai/records").json()
        assert listed and all("call_log" not in x for x in listed)
        # real-model path without a key is unchanged: 503
        app.state.model_factory = None
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert client.post("/ai/deviation-explanation", json={"scenario_kind": "base", "year": PLAN_YEAR}).status_code == 503


# --------------------------------------------------------------------------- defaults


def test_default_policy_comes_from_config(ai_db):
    assert DEFAULT_POLICY == ContextPolicy()
    assert DEFAULT_POLICY.summarization_trigger_tokens == config.AI_CONTEXT_SUMMARIZE_AT == 120_000
    assert DEFAULT_POLICY.summarization_keep_messages == config.AI_CONTEXT_SUMMARIZE_KEEP_MESSAGES == 6
    assert DEFAULT_POLICY.clear_tool_uses_trigger_tokens == config.AI_CONTEXT_CLEAR_TOOL_USES_AT == 60_000
    assert DEFAULT_POLICY.clear_tool_uses_keep == config.AI_CONTEXT_CLEAR_TOOL_USES_KEEP == 3
    assert DEFAULT_POLICY.tool_result_evict_tokens == config.AI_CONTEXT_TOOL_RESULT_EVICT_TOKENS == 4_000
    assert DEFAULT_POLICY.cache_ttl == config.AI_CONTEXT_CACHE_TTL == "5m"
    assert DEFAULT_POLICY.summarization_model is None

    fake = FakeToolCallingModel(responses=[])
    fs, clear, summ, cache = build_context_middleware(None, model=fake, backend=make_backend())
    assert fs.name == "FilesystemMiddleware" and fs._enabled_tools == frozenset({"read_file", "ls", "grep"})
    assert fs._tool_token_limit_before_evict == 4_000
    assert clear.edits[0].trigger == 60_000 and clear.edits[0].keep == 3 and tuple(clear.edits[0].exclude_tools) == CLEAR_EXCLUDED_TOOLS
    assert summ.name == "SummarizationMiddleware"
    assert summ._lc_helper.trigger == ("tokens", 120_000) and summ._lc_helper.keep == ("messages", 6)
    assert summ._lc_helper.model is fake  # None -> the agent's own model
    assert cache.ttl == "5m" and cache.unsupported_model_behavior == "ignore"

    with pytest.raises(ValueError):
        ContextPolicy(cache_ttl="2h")
    with pytest.raises(ValueError):
        ContextPolicy(tool_result_evict_tokens=0)


def test_nothing_written_under_repo_ai_dir(ai_db):
    """Belt and braces: after a normal touchpoint run the package directory is unchanged."""
    factory, _ = ai_db
    before = _repo_files()
    run_deviation_explanation(factory, scenario_kind="base", year=PLAN_YEAR, model=_deviation_script(2))
    assert _repo_files() == before
    assert not any(Path(p).name.startswith("session_") for p in before)
