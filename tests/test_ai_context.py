"""Context-window management + per-call audit, offline with the scripted fake model.

Covers: compiled middleware order (no ContextEditingMiddleware: tool results are evicted to a
file, never cleared), summarization with history offload (state only, never disk/DB),
tool-result eviction to /large_tool_results/ and the read_file way back, the persisted call
log, the API detail route, and the config defaults.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from nvplan import config
from nvplan.ai import ContextPolicy, DEFAULT_POLICY, build_touchpoint_agent, run_deviation_explanation, run_revenue_proposal
from nvplan.ai.audit import CALL_LOG_KEYS, EVICTED_MARKER
from nvplan.ai.context import SKILLS_DIR, build_context_middleware, make_backend, middleware_names
from nvplan.ai.fake import (
    FakeToolCallingModel,
    ScriptedDeviationModel,
    ai_calls,
    contributions_from_table,
    fake_summary_model,
    read_skill,
    scripted_deviation_model,
    scripted_revenue_model,
    structured,
    tool_call,
)
from nvplan.ai.schemas import DeviationExplanation, EnvScanResult
from nvplan.ai.figures import plan_vs_actual
from nvplan.ai.tools import AiRunContext, make_read_tools
from nvplan.api.app import create_app
from nvplan.db.models import Actual, AiRecord, Base, Category
from test_ai_fixtures import PLAN_YEAR, ai_db  # noqa: F401 (fixture)

AI_PKG_DIR = SKILLS_DIR.parent
HUGE = 10**9


def _repo_files() -> list[str]:
    return sorted(str(p) for p in AI_PKG_DIR.rglob("*") if "__pycache__" not in p.parts)


def _row_counts(factory) -> dict[str, int]:
    with factory() as s:
        return {t.name: s.scalar(select(func.count()).select_from(t)) for t in Base.metadata.sorted_tables}


def _deviation_script(n_tool_turns: int, factory, tool: str = "get_actuals") -> ScriptedDeviationModel:
    """Skill read, ``n_tool_turns`` reads of ``tool``, then a structured explanation whose figures
    come from the deterministic table (passed in: with summarization the tool result may be gone)."""
    with factory() as s:
        table = plan_vs_actual(s, "base", PLAN_YEAR)
    expl = DeviationExplanation(scenario="base", year=PLAN_YEAR, summary="scripted", contributions=contributions_from_table(table))
    turns = [read_skill("deviation_explanation")]
    turns += [ai_calls(tool_call(tool, {}, f"t{i}")) for i in range(n_tool_turns)]
    turns.append(structured("DeviationExplanation", expl.model_dump(), content="done"))
    return ScriptedDeviationModel(responses=turns, table=table)


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
        "ContextAuditMiddleware",
        "AnthropicPromptCachingMiddleware",
    ]
    # the audit is inside summarization, so it sees the rewritten request
    assert order.index("ContextAuditMiddleware") > order.index("SummarizationMiddleware")
    # eviction only, no clearing: a cleared data result would leave the model citing from memory
    assert "ContextEditingMiddleware" not in order
    print("compiled middleware order:", " > ".join(order))


# --------------------------------------------------------------------------- summarization


def test_summarization_fires_and_offloads_to_state_only(ai_db):
    factory, _ = ai_db
    files_before = _repo_files()
    rows_before = _row_counts(factory)
    ctx = AiRunContext()
    policy = ContextPolicy(
        summarization_trigger_tokens=6_000,
        summarization_keep_messages=2,
        tool_result_evict_tokens=HUGE,
        summarization_model=fake_summary_model("FAKE SUMMARY OF THE EARLIER TOOL READS"),
    )
    agent = build_touchpoint_agent(
        "deviation_explanation", model=_deviation_script(15, factory), session_factory=factory, extra_context=ctx, policy=policy
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
        tool_result_evict_tokens=HUGE,
        summarization_model=fake_summary_model(),
    )
    rec = run_deviation_explanation(factory, scenario_kind="base", year=PLAN_YEAR, model=_deviation_script(15, factory), policy=policy)
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
    policy = ContextPolicy(tool_result_evict_tokens=300, summarization_trigger_tokens=HUGE)
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


def test_evicted_actuals_are_readable_row_by_row(ai_db):
    """The data-bearing case the no-clearing decision is about: get_actuals is evicted, the
    pointer names the path, and read_file(offset, limit) on it returns the rows."""
    factory, _ = ai_db
    ctx = AiRunContext()
    with factory() as s:
        table = plan_vs_actual(s, "base", PLAN_YEAR)
    # what the tool returns: one row per line, so the PERS block is a contiguous page
    raw = {t.name: t for t in make_read_tools(factory)}["get_actuals"].invoke({})
    lines = raw.splitlines()
    pers_lines = [i for i, ln in enumerate(lines) if '"category_code": "PERS"' in ln]
    assert len(pers_lines) == 10 and pers_lines == list(range(pers_lines[0], pers_lines[0] + 10))
    assert len(raw) > 2_400 and max(map(len, lines)) < 200
    expl = DeviationExplanation(scenario="base", year=PLAN_YEAR, summary="read back", contributions=contributions_from_table(table))
    model = ScriptedDeviationModel(
        responses=[
            read_skill("deviation_explanation"),
            ai_calls(tool_call("get_actuals", {}, "act")),
            ai_calls(tool_call("read_file", {"file_path": "/large_tool_results/act", "offset": pers_lines[0], "limit": 10}, "rd")),
            structured("DeviationExplanation", expl.model_dump(), content="read back"),
        ],
        table=table,
    )
    # 600 tokens = 2400 chars: the 60-row actuals result (~5k chars) is evicted, the skill is
    # not, and a 10-line page of the offloaded file fits (read_file pages are capped by the
    # same limit).
    policy = ContextPolicy(tool_result_evict_tokens=600, summarization_trigger_tokens=HUGE)
    agent = build_touchpoint_agent("deviation_explanation", model=model, session_factory=factory, extra_context=ctx, policy=policy)
    result = agent.invoke({"messages": [{"role": "user", "content": "explain"}]})
    assert result["structured_response"].summary == "read back"

    tools = {m.tool_call_id: m for m in result["messages"] if m.type == "tool"}
    pointer = tools["act"].content
    assert EVICTED_MARKER in pointer and "/large_tool_results/act" in pointer and "read_file" in pointer
    assert "offset" in pointer and "limit" in pointer  # the pointer tells the model how to page
    assert "/large_tool_results/act" in result["files"]
    assert EVICTED_MARKER not in tools["skill-deviation_explanation"].content
    # one row per line: the read-back page holds the PERS rows incl. the 2025 value
    page = tools["rd"].content
    with factory() as s:
        pers_2025 = s.scalar(select(Actual.value).join(Category).where(Category.code == "PERS", Actual.year == PLAN_YEAR))
    assert '"category_code": "PERS"' in page and f'"year": {PLAN_YEAR}, "value": {pers_2025}' in page
    assert sum(1 for ln in page.splitlines() if '"PERS"' in ln) == 10  # 2016-2025, one line each
    assert '"REV"' not in page and '"OTH"' not in page  # offset/limit really paged
    # audit: the pointer, not the payload, was sent; nothing was ever cleared
    evicted_calls = [c for c in ctx.call_log if c["evicted_tool_results"]]
    assert evicted_calls and all("cleared_tool_results" not in c for c in ctx.call_log)


# --------------------------------------------------------------------------- no clearing


def test_no_tool_result_is_ever_cleared(ai_db):
    """Six data reads well above any clearing threshold that used to exist: every tool result
    still reaches the model verbatim (or as an eviction pointer), and the audit has no
    ``cleared`` flags or ``cleared_tool_results`` counter."""
    factory, _ = ai_db
    ctx = AiRunContext(model_version="fake")
    scan = EnvScanResult(flagged=[], summary="nothing cleared")
    turns = [read_skill("env_scan")]
    turns += [ai_calls(tool_call("get_parameters", {}, f"p{i}")) for i in range(6)]
    turns.append(structured("EnvScanResult", scan.model_dump(), content=scan.summary))
    policy = ContextPolicy(summarization_trigger_tokens=HUGE, tool_result_evict_tokens=HUGE)
    agent = build_touchpoint_agent(
        "env_scan", model=FakeToolCallingModel(responses=turns), session_factory=factory, extra_context=ctx, policy=policy
    )
    result = agent.invoke({"messages": [{"role": "user", "content": "scan"}]})
    assert result["structured_response"].summary == "nothing cleared"
    last = ctx.call_log[-1]
    assert "cleared_tool_results" not in last and "cleared_tool_results" not in CALL_LOG_KEYS
    tool_entries = [m for m in last["request"]["messages"] if m["role"] == "tool"]
    assert len(tool_entries) == 7
    assert all("cleared" not in e and "[cleared]" != e["content_excerpt"] for e in tool_entries)
    assert all('"alpha"' in e["content_excerpt"] for e in tool_entries if e["tool_name"] == "get_parameters")


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
        assert call["summarized"] is False and call["evicted_tool_results"] == 0 and "cleared_tool_results" not in call
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
    assert not hasattr(DEFAULT_POLICY, "clear_tool_uses_trigger_tokens") and not hasattr(config, "AI_CONTEXT_CLEAR_TOOL_USES_AT")
    assert DEFAULT_POLICY.tool_result_evict_tokens == config.AI_CONTEXT_TOOL_RESULT_EVICT_TOKENS == 4_000
    assert DEFAULT_POLICY.cache_ttl == config.AI_CONTEXT_CACHE_TTL == "5m"
    assert DEFAULT_POLICY.summarization_model is None

    fake = FakeToolCallingModel(responses=[])
    fs, summ, cache = build_context_middleware(None, model=fake, backend=make_backend())
    assert fs.name == "FilesystemMiddleware" and fs._enabled_tools == frozenset({"read_file", "ls", "grep"})
    assert fs._tool_token_limit_before_evict == 4_000
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
    run_deviation_explanation(factory, scenario_kind="base", year=PLAN_YEAR, model=_deviation_script(2, factory))
    assert _repo_files() == before
    assert not any(Path(p).name.startswith("session_") for p in before)
