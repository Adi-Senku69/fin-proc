"""Per-call audit of what was actually sent to the model (the compliance artefact).

The PDF rule is "the literal prompt used at any AI step is stored and viewable". With
summarization, tool-result eviction and tool-use clearing in play, the literal prompt is
different on every model call, so ``ai_record.prompt_text`` (system prompt + rendered user
prompt) is no longer the whole story. ``ContextAuditMiddleware`` records one entry per model
call in ``AiRunContext.call_log``; the entrypoints persist it in ``ai_record.call_log_json``
and ``GET /ai/records/{id}`` returns it as ``call_log``.

Position in the stack (verified empirically with the fake model, see
``tests/test_ai_context.py::test_middleware_order``): deepagents splices user middleware that
does not replace a default slot *after* its core stack, in the order given, ahead of the
prompt-caching tail. ``build_touchpoint_agent`` passes ``[*context middleware, audit]``, so
the compiled order is ``[Skills] Filesystem [SubAgent] Summarization PatchToolCalls
ContextEditing ContextAudit AnthropicPromptCaching``. For ``wrap_model_call`` the first entry
is outermost, so the audit sees the request *after* summarization rewrote it and *after*
ClearToolUsesEdit replaced old tool results, i.e. exactly the messages the provider gets
(prompt caching only adds cache-control metadata afterwards).

How each field is detected
--------------------------
* ``summarized``: the request's first message is the summary ``HumanMessage`` that deepagents'
  ``SummarizationMiddleware`` builds (``additional_kwargs["lc_source"] == "summarization"``),
  or - belt and braces - the request's first non-system message is not the first message in
  ``request.state["messages"]`` (summarization rewrites the request, never the state).
* ``cleared_tool_results``: ``ToolMessage``s whose content is the ``ClearToolUsesEdit``
  placeholder (``"[cleared]"``) or that carry ``response_metadata["context_editing"]["cleared"]``.
* ``evicted_tool_results``: ``ToolMessage``s whose text references ``/large_tool_results/``
  (the pointer deepagents' ``FilesystemMiddleware`` leaves behind after eviction).
* ``approx_tokens``: ``count_tokens_approximately`` over system + messages (chars/4 + 3 per
  message; no model call). Tool schemas are not included.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately

from nvplan.ai.tools import AiRunContext

EXCERPT_CHARS = 2000
SUMMARY_SOURCE = "summarization"
CLEARED_PLACEHOLDER = "[cleared]"
EVICTED_MARKER = "/large_tool_results/"

CALL_LOG_KEYS: tuple[str, ...] = (
    "call_index",
    "timestamp",
    "n_messages",
    "approx_tokens",
    "summarized",
    "cleared_tool_results",
    "evicted_tool_results",
    "tools_offered",
    "system_prompt_chars",
    "request",
    "response",
)


def message_text(content: Any) -> str:
    """Plain text of a message content (str or content blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(p for p in parts if p)
    return str(content) if content is not None else ""


def _tool_call_names(m: BaseMessage) -> list[str]:
    calls = getattr(m, "tool_calls", None) or []
    return [c.get("name", "") for c in calls]


def _is_summary_message(m: BaseMessage) -> bool:
    return m.type == "human" and m.additional_kwargs.get("lc_source") == SUMMARY_SOURCE


def _is_cleared(m: BaseMessage) -> bool:
    if not isinstance(m, ToolMessage):
        return False
    if m.response_metadata.get("context_editing", {}).get("cleared"):
        return True
    return message_text(m.content).strip() == CLEARED_PLACEHOLDER


def _is_evicted(m: BaseMessage) -> bool:
    return isinstance(m, ToolMessage) and EVICTED_MARKER in message_text(m.content)


def _summarized(request_messages: Sequence[BaseMessage], state: Any) -> bool:
    if request_messages and _is_summary_message(request_messages[0]):
        return True
    try:
        state_messages = list(state["messages"]) if state and "messages" in state else []
    except TypeError:
        state_messages = []
    if not request_messages or not state_messages:
        return False
    first_req = request_messages[0]
    first_state = next((m for m in state_messages if m.type != "system"), None)
    if first_state is None:
        return False
    return (first_req.id or first_req.content) != (first_state.id or first_state.content)


def _message_entry(m: BaseMessage) -> dict[str, Any]:
    entry: dict[str, Any] = {"role": m.type, "content_excerpt": message_text(m.content)[:EXCERPT_CHARS]}
    names = _tool_call_names(m)
    if names:
        entry["tool_calls"] = names
    if isinstance(m, ToolMessage):
        entry["tool_name"] = m.name
        entry["tool_call_id"] = m.tool_call_id
        if _is_cleared(m):
            entry["cleared"] = True
        if _is_evicted(m):
            entry["evicted"] = True
    if _is_summary_message(m):
        entry["summary"] = True
    return entry


def _response_entry(result: Sequence[BaseMessage] | None) -> dict[str, Any]:
    ai = next((m for m in (result or []) if isinstance(m, AIMessage)), None)
    if ai is None:
        return {"stop": None, "text_excerpt": "", "tool_calls": []}
    meta = ai.response_metadata or {}
    stop = meta.get("stop_reason") or meta.get("finish_reason") or ("tool_use" if ai.tool_calls else None)
    entry: dict[str, Any] = {
        "stop": stop,
        "text_excerpt": message_text(ai.content)[:EXCERPT_CHARS],
        "tool_calls": _tool_call_names(ai),
    }
    usage = getattr(ai, "usage_metadata", None)
    if usage:
        entry["usage"] = dict(usage)
    return entry


def _tool_name(t: Any) -> str:
    if isinstance(t, dict):
        return str(t.get("name") or t.get("function", {}).get("name") or t.get("type", "?"))
    return str(getattr(t, "name", type(t).__name__))


class ContextAuditMiddleware(AgentMiddleware):
    """Append one audit entry per model call to ``ctx.call_log`` (see module doc)."""

    def __init__(self, ctx: AiRunContext, *, excerpt_chars: int = EXCERPT_CHARS) -> None:
        super().__init__()
        self.ctx = ctx
        self.excerpt_chars = excerpt_chars

    def _record(self, request: Any, result: Sequence[BaseMessage] | None) -> None:
        messages = list(request.messages)
        system = request.system_message
        system_text = message_text(system.content) if system is not None else ""
        counted = ([system] if system is not None else []) + messages
        self.ctx.call_log.append(
            {
                "call_index": len(self.ctx.call_log),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "n_messages": len(messages),
                "approx_tokens": int(count_tokens_approximately(counted)),
                "summarized": _summarized(messages, request.state),
                "cleared_tool_results": sum(1 for m in messages if _is_cleared(m)),
                "evicted_tool_results": sum(1 for m in messages if _is_evicted(m)),
                "tools_offered": sorted(_tool_name(t) for t in (request.tools or [])),
                "system_prompt_chars": len(system_text),
                "request": {"system": system_text, "messages": [_message_entry(m) for m in messages]},
                "response": _response_entry(result),
            }
        )

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        response = handler(request)
        self._record(request, _result_messages(response))
        return response

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        response = await handler(request)
        self._record(request, _result_messages(response))
        return response


def _result_messages(response: Any) -> list[BaseMessage]:
    if isinstance(response, AIMessage):
        return [response]
    inner = getattr(response, "model_response", None)  # ExtendedModelResponse
    if inner is not None:
        response = inner
    return list(getattr(response, "result", None) or [])
