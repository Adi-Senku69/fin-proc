"""The real-model seam, offline: configuration, credential resolution, refusal handling, usage.

Nothing here calls the API. The live counterparts are in ``tests/test_live_model.py``
(marker ``live``, skipped without a credential).
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from sqlalchemy import func, select

from nvplan import config
from nvplan.ai import (
    MissingCredentials,
    ModelRefused,
    build_chat_model,
    credential_hint,
    credentials_available,
    get_model,
    model_version,
    run_deviation_explanation,
    run_env_scan,
    run_revenue_proposal,
    total_usage,
)
from nvplan.ai.agents import refusal_details, workspace_headers
from nvplan.ai.audit import CALL_LOG_KEYS, usage_entry
from nvplan.ai.fake import DeterministicChatModel, FakeToolCallingModel, deterministic_model, refusal, refusing_model, scripted_deviation_model, tool_call
from nvplan.api.app import create_app
from nvplan.db.models import AiRecord, ExternalNote
from test_ai_fixtures import PLAN_YEAR, ai_db  # noqa: F401 (fixture)

DUMMY_KEY = "sk-ant-api03-dummy-key-for-construction-only"


@pytest.fixture()
def no_credentials(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("nvplan.ai.agents._ant_cli", lambda: None)
    return None


@pytest.fixture()
def dummy_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", DUMMY_KEY)
    return DUMMY_KEY


# --------------------------------------------------------------------------- configuration


def test_config_defaults():
    assert config.AI_MODEL == "claude-opus-5"
    assert config.AI_EFFORT in config.AI_EFFORTS
    assert config.AI_MAX_TOKENS >= 16_000  # 8192 could truncate a 54-position env scan
    assert config.AI_BETAS == [] or all(isinstance(b, str) for b in config.AI_BETAS)


def _config_probe(code: str, **env: str) -> subprocess.CompletedProcess:
    """Import nvplan.config in a fresh interpreter with ``env`` set (import-time resolution)."""
    return subprocess.run(
        [sys.executable, "-c", "from nvplan import config\n" + code],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent-home", **env},
        cwd=str(config.PROJECT_ROOT),
    )


def test_every_ai_value_is_overridable_by_environment():
    out = _config_probe(
        "print(config.AI_MODEL, config.AI_EFFORT, config.AI_MAX_TOKENS, config.AI_BETAS)",
        NVPLAN_AI_MODEL="claude-sonnet-5",
        NVPLAN_AI_EFFORT="low",
        NVPLAN_AI_MAX_TOKENS="4096",
        NVPLAN_AI_BETAS="fast-mode-2026-02-01, other-beta",
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "claude-sonnet-5 low 4096 ['fast-mode-2026-02-01', 'other-beta']"


def test_bad_effort_and_bad_max_tokens_fail_loudly():
    bad_effort = _config_probe("print(config.AI_EFFORT)", NVPLAN_AI_EFFORT="turbo")
    assert bad_effort.returncode != 0
    assert "NVPLAN_AI_EFFORT='turbo'" in bad_effort.stderr and "low, medium, high, xhigh, max" in bad_effort.stderr

    bad_tokens = _config_probe("print(config.AI_MAX_TOKENS)", NVPLAN_AI_MAX_TOKENS="lots")
    assert bad_tokens.returncode != 0 and "NVPLAN_AI_MAX_TOKENS='lots'" in bad_tokens.stderr


def test_provider_defaults_to_auto_and_rejects_bad_values():
    assert config.AI_PROVIDER in config.AI_PROVIDERS
    bad = _config_probe("print(config.AI_PROVIDER)", NVPLAN_AI_PROVIDER="sometimes")
    assert bad.returncode != 0
    assert "NVPLAN_AI_PROVIDER='sometimes'" in bad.stderr and "auto, live, deterministic" in bad.stderr


def test_dotenv_is_loaded_at_import_without_overriding_the_environment():
    # .env holds a real key; an exported value must win over it, and a set var is never replaced.
    out = _config_probe(
        "import os; print(os.environ['ANTHROPIC_API_KEY'])",
        ANTHROPIC_API_KEY="sk-ant-exported-wins",
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "sk-ant-exported-wins"


def test_dotenv_absence_is_not_fatal(tmp_path):
    out = subprocess.run(
        [sys.executable, "-c", "from nvplan import config; print(config.AI_MODEL)"],
        capture_output=True, text=True, cwd=str(tmp_path),
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "PYTHONPATH": str(config.PROJECT_ROOT)},
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "claude-opus-5"


# --------------------------------------------------------------------------- credentials


def test_credentials_available_reads_the_environment_lazily(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", DUMMY_KEY)
    assert credentials_available() is True
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "oauth-token")
    assert credentials_available() is True


def test_no_credentials_gives_an_actionable_error(no_credentials):
    assert credentials_available() is False
    hint = credential_hint()
    assert "ANTHROPIC_API_KEY" in hint
    assert str(config.DOTENV_PATH) in hint
    assert config.AI_MODEL in hint
    assert "ANTHROPIC_WORKSPACE_ID is not a credential" in hint
    assert "nvplan-ai-check" in hint

    with pytest.raises(MissingCredentials) as exc:
        get_model()
    assert str(exc.value) == hint  # the API's 503 detail is this message verbatim


def test_workspace_id_is_a_header_not_a_credential(monkeypatch, no_credentials):
    """An organization-level key is rejected with a 400 unless the request carries the workspace
    header, so the value is forwarded - but on its own it is never a credential."""
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_live_check")
    assert credentials_available() is False  # not a credential
    assert workspace_headers() == {"anthropic-workspace-id": "wrkspc_live_check"}
    monkeypatch.setenv("ANTHROPIC_API_KEY", DUMMY_KEY)
    assert get_model().default_headers == {"anthropic-workspace-id": "wrkspc_live_check"}
    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID")
    assert workspace_headers() == {}
    assert not get_model().default_headers


def test_ant_cli_hint_appears_only_when_the_cli_exists(monkeypatch, no_credentials):
    assert "ant auth login" not in credential_hint()
    monkeypatch.setattr("nvplan.ai.agents._ant_cli", lambda: "/usr/local/bin/ant")
    assert "ant auth login" in credential_hint()


def test_ant_profile_counts_as_a_credential(monkeypatch, tmp_path, no_credentials):
    monkeypatch.setattr("nvplan.ai.agents._ant_cli", lambda: "/usr/local/bin/ant")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert credentials_available() is False
    (tmp_path / "anthropic").mkdir()
    (tmp_path / "anthropic" / "profiles.json").write_text("{}")
    assert credentials_available() is True


# --------------------------------------------------------------------------- get_model


def test_fake_model_is_passed_through_untouched():
    fake = FakeToolCallingModel(responses=[AIMessage(content="hi")])
    assert get_model(fake) is fake
    assert model_version(fake) == "fake"


def test_get_model_builds_opus_5_with_effort_and_max_tokens_only(dummy_key):
    chat = get_model()
    assert type(chat).__name__ == "ChatAnthropic"
    assert chat.model == config.AI_MODEL
    assert chat.max_tokens == config.AI_MAX_TOKENS
    assert chat.reasoning_effort == config.AI_EFFORT  # field alias: effort=
    assert chat.stream_usage is True  # so usage_metadata always comes back
    # Never set: Opus 5 rejects every sampling parameter with a 400, and budget_tokens is removed.
    assert chat.temperature is None and chat.top_p is None and chat.top_k is None
    assert chat.thinking is None

    payload = chat._get_request_payload([{"role": "user", "content": "x"}])
    assert payload["model"] == config.AI_MODEL
    assert payload["max_tokens"] == config.AI_MAX_TOKENS
    assert payload["output_config"] == {"effort": config.AI_EFFORT}
    assert "temperature" not in payload and "top_p" not in payload and "top_k" not in payload
    assert "budget_tokens" not in (payload.get("thinking") or {})


def test_get_model_honours_a_model_name_and_betas(dummy_key, monkeypatch):
    monkeypatch.setattr(config, "AI_BETAS", ["fast-mode-2026-02-01"])
    chat = get_model("claude-sonnet-5")
    assert chat.model == "claude-sonnet-5"
    assert chat.betas == ["fast-mode-2026-02-01"]
    assert "betas" not in build_chat_model(betas=[])._get_request_payload([{"role": "user", "content": "x"}])


def test_build_chat_model_overrides_are_cheap(dummy_key):
    chat = build_chat_model(max_tokens=64, effort="low")
    assert chat.max_tokens == 64 and chat.reasoning_effort == "low"
    with pytest.raises(ValueError, match="turbo"):
        build_chat_model(effort="turbo")


# --------------------------------------------------------------------------- resolve_model (A1)


def test_resolve_model_passes_a_basechatmodel_through_regardless_of_provider(monkeypatch):
    from nvplan.ai.agents import resolve_model

    fake = FakeToolCallingModel(responses=[])
    for provider in ("auto", "live", "deterministic"):
        monkeypatch.setattr(config, "AI_PROVIDER", provider, raising=False)
        assert resolve_model(fake) is fake


def test_resolve_model_auto_picks_deterministic_without_a_credential(no_credentials):
    from nvplan.ai.agents import resolve_model

    assert isinstance(resolve_model(None), DeterministicChatModel)


def test_resolve_model_auto_picks_the_real_client_with_a_credential(dummy_key):
    from nvplan.ai.agents import resolve_model

    assert type(resolve_model(None)).__name__ == "ChatAnthropic"


def test_resolve_model_deterministic_override_ignores_a_present_credential(dummy_key, monkeypatch):
    from nvplan.ai.agents import resolve_model

    monkeypatch.setattr(config, "AI_PROVIDER", "deterministic", raising=False)
    assert isinstance(resolve_model(None), DeterministicChatModel)


def test_resolve_model_live_override_still_raises_without_a_credential(no_credentials, monkeypatch):
    from nvplan.ai.agents import resolve_model

    monkeypatch.setattr(config, "AI_PROVIDER", "live", raising=False)
    with pytest.raises(MissingCredentials):
        resolve_model(None)


# --------------------------------------------------------------------------- the deterministic provider itself


def _bound(*names: str) -> DeterministicChatModel:
    """A fresh deterministic model bound to tool names the way deepagents binds real ones -
    a plain object carrying ``.name`` is enough, since DeterministicChatModel.bind_tools only
    ever reads that attribute (see nvplan.ai.fake._tool_name)."""
    class _Named:
        def __init__(self, name: str) -> None:
            self.name = name

    return deterministic_model().bind_tools([_Named(n) for n in names])


def _tool_msg(name: str, call_id: str, payload) -> "ToolMessage":
    from langchain_core.messages import ToolMessage

    return ToolMessage(content=json.dumps(payload), tool_call_id=call_id, name=name)


def test_deterministic_revenue_proposal_clamps_to_the_control_table_bound():
    """The one guardrail this touchpoint has (control_table.yaml: max_deviation_from_default_pct)
    must actually bind on the deterministic provider's own arithmetic, not only on a scripted
    test double - proving it is "a real quality floor", not a stub."""
    from langchain_core.messages import HumanMessage

    model = _bound("get_actuals", "get_external_notes", "get_control_table", "record_revenue_proposal", "RevenueProposal")
    human = HumanMessage(
        "Scenario: base. Target year: 2027.\n\nValorized default revenue for 2027: 20,000.0 k EUR.\n"
    )
    first = model.invoke([human])
    assert {c["name"] for c in first.tool_calls} == {"get_actuals", "get_external_notes", "get_control_table"}

    history = [
        human,
        first,
        _tool_msg("get_actuals", "det-act", {"rows": []}),
        _tool_msg(
            "get_external_notes",
            "det-notes",
            {"rows": [{"id": 7, "category_code": "REV", "year": 2027, "text": "Contract ends; a 90% reduction expected."}]},
        ),
        _tool_msg("get_control_table", "det-rules", {"revenue_proposal": {"max_deviation_from_default_pct": 25, "must_cite_note": True}}),
    ]
    second = model.invoke(history)
    assert second.tool_calls[0]["name"] == "record_revenue_proposal"
    args = second.tool_calls[0]["args"]
    assert args["cited_note_ids"] == [7]
    # a live model reading "90% reduction" might propose exactly that; the deterministic
    # provider must clamp to the control table's 25% bound so record_revenue_proposal never
    # rejects its own default.
    assert args["proposed_value"] == pytest.approx(20_000.0 * 0.75, rel=1e-3)

    history += [second, _tool_msg("record_revenue_proposal", "det-prop", {"ai_record_id": 1, "status": "proposed"})]
    third = model.invoke(history)
    assert third.tool_calls[0]["name"] == "RevenueProposal"
    assert third.tool_calls[0]["args"]["proposed_value"] == pytest.approx(args["proposed_value"])
    assert third.tool_calls[0]["args"]["cited_note_ids"] == [7]


def test_deterministic_revenue_proposal_leaves_default_unchanged_without_a_quantified_note():
    from langchain_core.messages import HumanMessage

    model = _bound("get_actuals", "get_external_notes", "get_control_table", "record_revenue_proposal", "RevenueProposal")
    human = HumanMessage("Scenario: base. Target year: 2028.\n\nValorized default revenue for 2028: 15,500.0 k EUR.\n")
    first = model.invoke([human])
    history = [
        human,
        first,
        _tool_msg("get_actuals", "det-act", {"rows": []}),
        _tool_msg("get_external_notes", "det-notes", {"rows": [{"id": 3, "category_code": "PERS", "year": 2028, "text": "wage rise"}]}),
        _tool_msg("get_control_table", "det-rules", {"revenue_proposal": {"max_deviation_from_default_pct": 25, "must_cite_note": True}}),
    ]
    second = model.invoke(history)
    args = second.tool_calls[0]["args"]
    assert args["proposed_value"] == pytest.approx(15_500.0)
    assert args["cited_note_ids"] == [3]  # must_cite_note satisfied even with nothing REV-specific


# --------------------------------------------------------------------------- refusal handling


def test_refusal_details_reads_stop_reason_and_details():
    assert refusal_details(AIMessage(content="ok")) is None
    assert refusal_details(AIMessage(content="", response_metadata={"stop_reason": "end_turn"})) is None
    details = refusal_details(refusal(category="cyber", explanation="nope"))
    assert details == {"category": "cyber", "explanation": "nope"}
    # stop_details is null for every other stop reason, so a bare refusal must not crash
    assert refusal_details(AIMessage(content="", response_metadata={"stop_reason": "refusal"})) == {
        "category": None, "explanation": None
    }


def _counts(factory) -> tuple[int, int]:
    with factory() as s:
        return (
            s.scalar(select(func.count()).select_from(AiRecord)),
            s.scalar(select(func.count()).select_from(ExternalNote)),
        )


def test_revenue_proposal_refusal_persists_nothing(ai_db):
    factory, _ = ai_db
    before = _counts(factory)
    with pytest.raises(ModelRefused) as exc:
        run_revenue_proposal(factory, scenario_kind="base", year=2027, default_value=22_000.0,
                             model=refusing_model(category="cyber", explanation="declined by a classifier"))
    assert "refusal" in str(exc.value) and "cyber" in str(exc.value) and "declined by a classifier" in str(exc.value)
    assert "nothing was persisted" in str(exc.value)
    assert _counts(factory) == before


def test_refusal_after_the_write_tool_already_committed_is_rolled_back(ai_db):
    """``record_revenue_proposal`` commits during the run, so a later refusal must delete it."""
    factory, _ = ai_db
    before = _counts(factory)
    with factory() as s:
        note_id = int(s.scalars(select(ExternalNote.id)).first())
    wrote = tool_call(
        "record_revenue_proposal",
        {"year": 2027, "proposed_value": 21_000.0, "rationale": "scripted", "cited_note_ids": [note_id]},
        "prop",
    )
    model = refusing_model(before=[AIMessage(content="", tool_calls=[wrote])])
    with pytest.raises(ModelRefused):
        run_revenue_proposal(factory, scenario_kind="base", year=2027, default_value=22_000.0, model=model)
    assert _counts(factory) == before  # the row the tool inserted is gone again


def test_env_scan_refusal_persists_neither_record_nor_notes(ai_db):
    factory, _ = ai_db
    before = _counts(factory)
    wrote = tool_call(
        "record_external_note",
        {"text": "scripted finding", "domain": "D2 Economic", "position": "D2.P4 Wage growth"},
        "note-0",
    )
    with pytest.raises(ModelRefused):
        run_env_scan(factory, model=refusing_model(before=[AIMessage(content="", tool_calls=[wrote])]))
    assert _counts(factory) == before


def test_deviation_explanation_refusal_persists_nothing(ai_db):
    factory, _ = ai_db
    before = _counts(factory)
    with pytest.raises(ModelRefused):
        run_deviation_explanation(factory, scenario_kind="base", year=PLAN_YEAR, model=refusing_model())
    assert _counts(factory) == before


def test_api_maps_a_refusal_to_502(ai_db):
    factory, _ = ai_db
    app = create_app(session_factory=factory)
    app.state.model_factory = lambda touchpoint, context: refusing_model()
    with TestClient(app) as client:
        r = client.post("/ai/deviation-explanation", json={"scenario_kind": "base", "year": PLAN_YEAR})
        assert r.status_code == 502
        assert "declined" in r.json()["detail"] and "no ai_record was written" in r.json()["detail"]
    with factory() as s:
        assert s.scalar(select(func.count()).select_from(AiRecord)) == 0


# --------------------------------------------------------------------------- usage in the audit log


def test_usage_entry_normalizes_langchain_usage_metadata():
    assert usage_entry(AIMessage(content="x")) is None  # the fake-model case
    message = AIMessage(
        content="x",
        usage_metadata={
            "input_tokens": 1200,
            "output_tokens": 40,
            "total_tokens": 1240,
            "input_token_details": {"cache_read": 1000, "cache_creation": 150},
        },
    )
    assert usage_entry(message) == {
        "input_tokens": 1200,
        "output_tokens": 40,
        "total_tokens": 1240,
        "cache_read_tokens": 1000,
        "cache_creation_tokens": 150,
    }


def test_total_usage_sums_over_calls_and_ignores_missing():
    log = [
        {"usage": {"input_tokens": 100, "output_tokens": 10, "cache_creation_tokens": 90}},
        {"usage": {"input_tokens": 120, "output_tokens": 20, "cache_read_tokens": 90}},
        {"usage": None},
    ]
    assert total_usage(log) == {
        "input_tokens": 220,
        "output_tokens": 30,
        "cache_creation_tokens": 90,
        "cache_read_tokens": 90,
    }
    assert total_usage([]) == {}


def test_call_log_carries_a_usage_key_that_is_null_with_the_fake_model(ai_db):
    factory, _ = ai_db
    model = scripted_deviation_model(scenario_kind="base", year=PLAN_YEAR, summary="scripted")
    rec = run_deviation_explanation(factory, scenario_kind="base", year=PLAN_YEAR, model=model)
    with factory() as s:
        log = s.get(AiRecord, rec.id).call_log_json
    assert log and "usage" in CALL_LOG_KEYS
    assert all("usage" in entry for entry in log)
    assert all(entry["usage"] is None for entry in log)  # documented: no usage_metadata offline
    assert all(entry["approx_tokens"] > 0 for entry in log)  # the offline estimate still works
    assert total_usage(log) == {}


# --------------------------------------------------------------------------- nvplan-ai-check


def test_ai_check_without_credentials_prints_the_hint_and_the_configuration(no_credentials, capsys):
    from nvplan.ai.check import main

    assert main([]) == 1  # exit 1, nothing sent
    out = capsys.readouterr().out
    assert "NO CREDENTIAL - nothing was sent." in out
    assert credential_hint() in out
    # the resolved configuration is printed with or without a credential
    assert config.AI_MODEL in out and config.AI_EFFORT in out and f"{config.AI_MAX_TOKENS:,}" in out
    assert "never sent: temperature / top_p / top_k" in out


def test_ai_check_seeds_a_demo_shaped_database(tmp_path):
    """The --touchpoint database: the demo's own seeding, no model involved."""
    from nvplan.api import queries as q
    from nvplan.ai.check import PROPOSAL_YEAR, seed_demo_db

    factory = seed_demo_db(tmp_path / "check.db", with_backtest_plan=False)
    with factory() as s:
        counts = q.table_counts(s)
        assert counts["actual"] > 0 and counts["plan_value"] > 0 and counts["external_note"] > 0
        assert counts["ai_record"] == 0
        assert q.default_revenue(s, "base", PROPOSAL_YEAR) > 0
