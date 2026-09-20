"""The end-to-end demo runs offline with the fakes and tells the whole story."""

from __future__ import annotations

import sys
import time

import pytest
from sqlalchemy.orm import sessionmaker

from nvplan import config
from nvplan.ai import agents
from nvplan.ai.agents import MissingCredentials, run_env_scan
from nvplan.api import demo
from nvplan.api.demo import main
from nvplan.api.queries import table_counts
from nvplan.db.session import get_engine


def test_demo_runs_with_fakes(tmp_path, capsys):
    db = tmp_path / "demo.db"
    t0 = time.perf_counter()
    assert main(db_path=str(db), use_fake_ai=True) == 0
    elapsed = time.perf_counter() - t0
    out = capsys.readouterr().out
    assert db.exists()
    with sessionmaker(bind=get_engine(f"sqlite:///{db}"))() as s:
        counts = table_counts(s)
    assert "ILLUSTRATIVE" in out and "SCRIPTED FAKE" in out
    assert "Personnel costs · 2028 · Base" in out and "## Backtest" in out
    assert "confirmed_by=C. Andres" in out and "prompt (verbatim)" in out
    assert "consistency: OK" in out and "deviation explanation -> ai_record" in out
    # three plan runs (first, confirmed rerun, backtest fit) x three scenarios; three ai records
    assert counts["scenario"] == 9 and counts["ai_record"] == 3 and counts["parameter"] == 12
    assert counts["actual"] == 60 and counts["external_note"] >= 4
    assert elapsed < 30, f"demo took {elapsed:.1f}s"


def _no_network(*args, **kwargs):  # any attempt to build the real model is a test failure
    raise AssertionError("the demo tried to build the real model / go to the network")


def test_demo_defaults_to_fakes_even_with_a_credential(tmp_path, monkeypatch, capsys):
    """A plain ``nvplan-demo`` (no flags) must stay on the scripted path even with a key in the
    environment - .env is auto-loaded at config import, so anything else is a silent bill."""
    db = tmp_path / "default.db"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
    monkeypatch.setattr(agents, "build_chat_model", _no_network)
    monkeypatch.setattr(sys, "argv", ["nvplan-demo", "--db", str(db)])
    assert main() == 0  # argv path: no --live
    out = capsys.readouterr().out
    assert "SCRIPTED FAKE MODELS" in out and "REAL TOKENS" not in out
    assert db.exists()


def test_fake_ai_flag_is_an_accepted_no_op(tmp_path, monkeypatch, capsys):
    db = tmp_path / "fakeflag.db"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
    monkeypatch.setattr(agents, "build_chat_model", _no_network)
    monkeypatch.setattr(sys, "argv", ["nvplan-demo", "--db", str(db), "--fake-ai"])
    assert main() == 0
    assert "SCRIPTED FAKE MODELS" in capsys.readouterr().out


def test_use_fake_ai_false_does_not_go_live(tmp_path, monkeypatch, capsys):
    """``use_fake_ai=False`` is not an opt-in: only ``live=True`` is."""
    db = tmp_path / "notlive.db"
    monkeypatch.setattr(agents, "build_chat_model", _no_network)
    monkeypatch.setattr(demo, "credentials_available", lambda: True)
    assert demo.run_demo(str(db), use_fake_ai=False)["ai_record"] == 3
    assert "SCRIPTED FAKE MODELS" in capsys.readouterr().out


def test_live_without_credential_fails_fast(tmp_path, monkeypatch, capsys):
    db = tmp_path / "live.db"
    monkeypatch.setattr(demo, "credentials_available", lambda: False)
    monkeypatch.setattr(agents, "build_chat_model", _no_network)
    monkeypatch.setattr(sys, "argv", ["nvplan-demo", "--db", str(db), "--live"])
    assert main() == 2  # non-zero, no silent fallback to the fakes
    out = capsys.readouterr().out
    assert "No Anthropic credential found" in out and "ANTHROPIC_API_KEY" in out
    assert not db.exists()  # it failed before any work
    # programmatic opt-in raises instead of falling back
    with pytest.raises(MissingCredentials):
        demo.run_demo(str(db), live=True)


def test_demo_does_not_default_env_scan_to_the_real_brain_root(tmp_path, monkeypatch, capsys):
    """The demo must not let ``run_env_scan`` fall back to its default ``brain_root`` (B2:
    ``config.BRAIN_ROOT``, the repo's real ``brain/`` tree) - a fresh-clone ``nvplan-demo`` run
    would otherwise leave an untracked ingestion record behind (observed:
    ``brain/ingestion/market/2026-09-20-env-scan-1.md``).

    ``tests/conftest.py`` redirects ``config.BRAIN_ROOT`` to a temp dir for the *whole* pytest
    run, precisely so ordinary tests never touch the real tree - which also means a check of
    "did anything land in ``config.BRAIN_ROOT``" can't tell a bare default from an explicit temp
    path passed by the demo; both are temp dirs by the time this test runs. So this asserts on
    what the demo actually calls ``run_env_scan`` with: without the fix, ``demo.py`` passes no
    ``brain_root`` at all (the spy sees ``None``, i.e. "let it default"); with the fix it passes
    its own scratch dir, which is never ``config.BRAIN_ROOT``."""
    db = tmp_path / "brainroot.db"
    seen_brain_roots: list[object] = []

    def spy(*args, **kwargs):
        seen_brain_roots.append(kwargs.get("brain_root"))
        return run_env_scan(*args, **kwargs)

    monkeypatch.setattr(demo, "run_env_scan", spy)
    assert demo.run_demo(str(db), use_fake_ai=True)["ai_record"] == 3
    assert seen_brain_roots, "run_env_scan was never called"
    assert seen_brain_roots[0] is not None, "demo called run_env_scan with no brain_root - it will default to config.BRAIN_ROOT"
    assert seen_brain_roots[0] != config.BRAIN_ROOT, "demo pointed run_env_scan straight at config.BRAIN_ROOT"
