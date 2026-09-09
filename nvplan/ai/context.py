"""Context-window management for the AI touchpoints (deepagents middleware).

Pinned against deepagents 0.7.13 / langchain 1.4 / langchain-anthropic 1.7.1.

What runs, in effect order (outermost first, as ``create_deep_agent`` compiles it;
``build_touchpoint_agent`` prints/asserts this in tests):

1. ``SkillsMiddleware`` (only with ``skills=``) - appends the skill index to the system prompt.
2. ``FilesystemMiddleware`` (ours replaces the default by ``.name``) - read-only tools
   ``read_file``/``ls``/``grep`` and **tool-result eviction**: any tool result longer than
   ``tool_result_evict_tokens`` (x4 chars) is written to ``/large_tool_results/<tool_call_id>``
   on the backend *at tool time* and the ``ToolMessage`` becomes a pointer + preview. The
   model reads the file selectively with ``read_file(offset, limit)``. Filesystem tools are
   never evicted (they are the way back).
3. ``SubAgentMiddleware`` (advisor only).
4. deepagents ``SummarizationMiddleware`` (ours replaces the default) - when system + messages
   + tool schemas exceed ``summarization_trigger_tokens`` (approximate count, never a model
   call) the older messages are summarized by ``summarization_model`` (or the agent's own),
   the evicted history is offloaded to ``/conversation_history/<session>.md`` on the
   backend, and the *request* becomes ``[summary HumanMessage, last K messages]``. State
   (``result["messages"]``) is left untouched; the event is kept in private state.
5. ``PatchToolCallsMiddleware`` (deepagents default).
6. ``ContextEditingMiddleware(ClearToolUsesEdit)`` (new entry, spliced after the core) -
   request-only: above ``clear_tool_uses_trigger_tokens`` every ``ToolMessage`` except the
   last ``clear_tool_uses_keep`` becomes ``"[cleared]"``. The two write tools are excluded so
   the model always sees what it recorded (note ids, ai_record id, validation errors).
7. ``ContextAuditMiddleware`` (nvplan.ai.audit; new entry) - sees the request after 4 and 6.
8. ``AnthropicPromptCachingMiddleware`` (ours replaces the tail default) - cache breakpoints
   on the system prompt and last tool; no-op for non-Anthropic models.

Ordering caveat (deepagents): user middleware that does not replace a default slot is
spliced *after* the core stack, so ``ContextEditingMiddleware`` runs inside
``SummarizationMiddleware``. Summarization therefore counts the un-cleared history; clearing
reduces what the model sees between the two thresholds, it does not delay summarization.

Backend: ``CompositeBackend(default=StateBackend(), routes={"/skills/": FilesystemBackend(
root_dir=nvplan/ai/skills, virtual_mode=True)})``. Offloads (``/conversation_history/``,
``/large_tool_results/``) land in graph state (``result["files"]``) - never on disk, never in
the DB - while ``/skills/<name>/SKILL.md`` is read from the repo. ``CompositeBackend`` strips
the route prefix, so the route root is the ``skills`` directory itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel

from nvplan import config

SKILLS_DIR = Path(__file__).resolve().parent / "skills"
SKILLS_SOURCE = "/skills/"
SKILL_NAMES: tuple[str, ...] = ("env-scan-54-positions", "revenue-proposal-method", "deviation-explanation-method")

# Tools whose results are never cleared by ClearToolUsesEdit (the model must keep seeing
# what it wrote and any validation error it has to fix).
CLEAR_EXCLUDED_TOOLS: tuple[str, ...] = ("record_revenue_proposal", "record_external_note")
READ_ONLY_FS_TOOLS: list[str] = ["read_file", "ls", "grep"]


@dataclass(frozen=True)
class ContextPolicy:
    """Thresholds for the context middleware. All token figures are approximate
    (``count_tokens_approximately``: chars/4 + 3 per message)."""

    summarization_trigger_tokens: int = config.AI_CONTEXT_SUMMARIZE_AT
    summarization_keep_messages: int = config.AI_CONTEXT_SUMMARIZE_KEEP_MESSAGES
    clear_tool_uses_trigger_tokens: int = config.AI_CONTEXT_CLEAR_TOOL_USES_AT
    clear_tool_uses_keep: int = config.AI_CONTEXT_CLEAR_TOOL_USES_KEEP
    tool_result_evict_tokens: int = config.AI_CONTEXT_TOOL_RESULT_EVICT_TOKENS
    cache_ttl: str = config.AI_CONTEXT_CACHE_TTL
    summarization_model: BaseChatModel | None = None  # None -> the agent's own model

    def __post_init__(self) -> None:
        if self.cache_ttl not in ("5m", "1h"):
            raise ValueError(f"cache_ttl must be '5m' or '1h', got {self.cache_ttl!r}")
        for name in ("summarization_trigger_tokens", "clear_tool_uses_trigger_tokens", "tool_result_evict_tokens"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")


DEFAULT_POLICY = ContextPolicy()


def make_backend(skills_dir: Path | None = None) -> Any:
    """In-memory state for everything, the repo's skills directory read-only under /skills/."""
    from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend

    return CompositeBackend(
        default=StateBackend(),
        routes={SKILLS_SOURCE: FilesystemBackend(root_dir=skills_dir or SKILLS_DIR, virtual_mode=True)},
    )


def build_context_middleware(policy: ContextPolicy | None, *, model: BaseChatModel, backend: Any) -> list[Any]:
    """The context stack for one agent, in the order handed to ``create_deep_agent``.

    Filesystem, Summarization and PromptCaching replace deepagents' defaults by name;
    ContextEditing is a new entry (see module doc for the compiled order).
    """
    from deepagents.middleware.filesystem import FilesystemMiddleware
    from deepagents.middleware.summarization import SummarizationMiddleware
    from langchain.agents.middleware import ClearToolUsesEdit, ContextEditingMiddleware
    from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware
    from langchain_core.messages.utils import count_tokens_approximately

    policy = policy or DEFAULT_POLICY
    return [
        FilesystemMiddleware(
            backend=backend,
            tools=list(READ_ONLY_FS_TOOLS),
            tool_token_limit_before_evict=policy.tool_result_evict_tokens,
        ),
        ContextEditingMiddleware(
            edits=[
                ClearToolUsesEdit(
                    trigger=policy.clear_tool_uses_trigger_tokens,
                    keep=policy.clear_tool_uses_keep,
                    exclude_tools=CLEAR_EXCLUDED_TOOLS,
                )
            ],
            token_counter=count_tokens_approximately,
        ),
        SummarizationMiddleware(
            model=policy.summarization_model or model,
            backend=backend,
            trigger=("tokens", policy.summarization_trigger_tokens),
            keep=("messages", policy.summarization_keep_messages),
            token_counter=count_tokens_approximately,
        ),
        AnthropicPromptCachingMiddleware(ttl=policy.cache_ttl, unsupported_model_behavior="ignore"),
    ]


def middleware_names(agent: Any) -> list[str]:
    """``.name`` of every middleware with a ``wrap_model_call`` hook in a compiled agent, in
    effect order (outermost first).

    langchain's ``create_agent`` keeps no middleware list on the compiled graph; it composes
    the ``wrap_model_call`` hooks into nested ``composed(outer, inner)`` closures on the
    ``model`` node. This walks those closures (``outer`` / ``inner`` / ``single_handler``
    free variables) and reads each hook's traced name ``"<Middleware>.wrap_model_call"``.
    """
    fn = agent.nodes["model"].bound.func
    cells = dict(zip(fn.__code__.co_freevars, (c.cell_contents for c in fn.__closure__ or ())))
    names: list[str] = []

    def hook_name(h: Any) -> str:
        n = getattr(h, "__name__", "") or ""
        if ".wrap_model_call" in n:
            return n.split(".wrap_model_call", 1)[0]
        owner = getattr(getattr(h, "__wrapped__", h), "__self__", None)
        return getattr(owner, "name", type(owner).__name__ if owner else n)

    def walk(h: Any) -> None:
        if h is None or not hasattr(h, "__code__"):
            return
        free = dict(zip(h.__code__.co_freevars, (c.cell_contents for c in h.__closure__ or ())))
        if "outer" in free and "inner" in free:
            names.append(hook_name(free["outer"]))
            walk(free["inner"])
        elif "single_handler" in free:
            names.append(hook_name(free["single_handler"]))
        else:
            names.append(hook_name(h))

    walk(cells.get("wrap_model_call_handler"))
    return names
