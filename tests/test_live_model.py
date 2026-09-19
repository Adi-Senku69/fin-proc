"""Live tests: they talk to the real Anthropic API and cost money.

Not part of the default run. ``addopts = -m 'not live'`` in ``pyproject.toml`` deselects them,
and every test here is additionally skipped when no credential is resolvable, so a checkout
without a key stays green either way::

    uv run pytest              # offline, these are deselected
    uv run pytest -m live      # runs them (needs ANTHROPIC_API_KEY in .env or the environment)

Kept deliberately small: four cheap calls at ``effort="low"`` with tiny ``max_tokens``. What they
pin down is the seam that no fake can cover - that the configured client is accepted by the API,
that structured output works against a real model, that ``model_version`` is a real model id
(never ``"fake"``), that ``usage_metadata`` comes back, and that ``effort`` / ``max_tokens``
reach the request body. The full touchpoints are exercised by ``nvplan-ai-check --touchpoint``,
which is a human-run command, not a paid test.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from nvplan import config
from nvplan.ai import build_chat_model, credentials_available, get_model, model_version
from nvplan.ai.audit import usage_entry

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not credentials_available(),
        reason="no Anthropic credential (ANTHROPIC_API_KEY in .env or the environment); see nvplan-ai-check",
    ),
]

CHEAP_MAX_TOKENS = 256
CHEAP_EFFORT = "low"


class Sum(BaseModel):
    """Trivial structured-output schema (one real call, no ambiguity)."""

    total: int = Field(description="the sum of the two numbers")


@pytest.fixture(scope="module")
def cheap_model():
    """The configured client, with the cheapest settings that still exercise the real path."""
    return build_chat_model(max_tokens=CHEAP_MAX_TOKENS, effort=CHEAP_EFFORT)


def test_structured_output_returns_the_pydantic_schema(cheap_model):
    result = cheap_model.with_structured_output(Sum).invoke("What is 17 + 25? Use the tool.")
    assert isinstance(result, Sum)
    assert result.total == 42


def test_model_version_is_a_real_model_id(cheap_model):
    version = model_version(cheap_model)
    assert version != "fake"
    assert version.startswith("claude")
    assert model_version(get_model()) == config.AI_MODEL  # what lands in ai_record.model_version


def test_usage_metadata_is_populated(cheap_model):
    response = cheap_model.invoke("Reply with exactly one word: ready")
    usage = usage_entry(response)
    assert usage is not None, "stream_usage defaults to True, so a real response must carry usage_metadata"
    assert usage["input_tokens"] > 0 and usage["output_tokens"] > 0
    # Cache figures are only reported when caching is in play; when present they must be sane.
    for key in ("cache_read_tokens", "cache_creation_tokens"):
        assert usage.get(key, 0) >= 0
    assert (response.response_metadata or {}).get("stop_reason") in ("end_turn", "max_tokens", "stop_sequence")


def test_effort_and_max_tokens_reach_the_request(cheap_model):
    payload = cheap_model._get_request_payload([{"role": "user", "content": "x"}])
    assert payload["max_tokens"] == CHEAP_MAX_TOKENS
    assert payload["output_config"]["effort"] == CHEAP_EFFORT
    assert not {"temperature", "top_p", "top_k"} & set(payload)  # Opus 5 would answer 400
    assert "budget_tokens" not in (payload.get("thinking") or {})

    configured = get_model()._get_request_payload([{"role": "user", "content": "x"}])
    assert configured["max_tokens"] == config.AI_MAX_TOKENS
    assert configured["output_config"]["effort"] == config.AI_EFFORT
    # The API accepts that exact body: a one-token call with the configured effort/model.
    response = get_model().invoke("Reply with exactly one word: ready")
    assert usage_entry(response) is not None
