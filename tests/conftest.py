import shutil
import tempfile
from pathlib import Path

import pytest

from nvplan.db.session import SessionLocal, get_engine, init_db, seed_categories

# B2: nvplan.ai.agents.run_env_scan can now write a real brain/ingestion/ markdown file (default
# target: nvplan.config.BRAIN_ROOT, the repository's real brain/ tree). Redirected here, once, for
# the WHOLE pytest run, to a throwaway directory - so the ordinary offline suite never writes into
# (or is affected by anything already sitting in) the real repo tree just because some test calls
# an env-scan entrypoint with no explicit brain_root.
#
# This is a pytest_configure hook, not a fixture, and deliberately not an autouse fixture: several
# existing fixtures that indirectly call run_env_scan with no brain_root (tests/test_api.py's
# `client`, tests/test_evals.py's `outcomes`) are module- or session-scoped and run their own
# setup before any function-scoped autouse fixture of the first test in their module ever fires -
# scope order in pytest sets up the broader-scoped fixture first, regardless of declaration order.
# A function-scoped monkeypatch tried here first and missed exactly that case (a real file landed
# in brain/ingestion/market/ from a module-scoped fixture's setup before the patch ran). Applying
# the redirect at collection time, before any fixture at all, closes that gap for every scope.
_BRAIN_ROOT_TMP: str | None = None


def pytest_configure(config):  # noqa: ARG001 - required hook signature
    global _BRAIN_ROOT_TMP
    import nvplan.config as _config

    _BRAIN_ROOT_TMP = tempfile.mkdtemp(prefix="nvplan-pytest-brain-")
    _config.BRAIN_ROOT = Path(_BRAIN_ROOT_TMP) / "brain"


def pytest_unconfigure(config):  # noqa: ARG001 - required hook signature
    if _BRAIN_ROOT_TMP is not None:
        shutil.rmtree(_BRAIN_ROOT_TMP, ignore_errors=True)


@pytest.fixture()
def engine():
    eng = get_engine("sqlite:///:memory:")
    init_db(eng)
    return eng


@pytest.fixture()
def session(engine):
    with SessionLocal(bind=engine) as s:
        seed_categories(s)
        yield s
