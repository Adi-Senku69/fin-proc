"""Context-window management for the AI touchpoints (deepagents middleware).

Pinned against deepagents 0.7.13 / langchain 1.4 / langchain-anthropic 1.7.1.

What runs, in effect order (outermost first, as ``create_deep_agent`` compiles it;
``build_touchpoint_agent`` prints/asserts this in tests):

1. ``SkillsMiddleware`` (only with ``skills=``) - appends the skill index to the system prompt.
2. ``FilesystemMiddleware`` (ours replaces the default by ``.name``) - read-only tools
   ``read_file``/``ls``/``grep`` and **tool-result eviction**: any tool result longer than
   ``tool_result_evict_tokens`` (x4 chars) is written to ``/large_tool_results/<tool_call_id>``
   on the backend *at tool time* and the ``ToolMessage`` becomes a pointer ("saved in the
   filesystem at this path: ...") + head/tail preview. The model reads the file selectively
   with ``read_file(offset, limit)`` (line-based; row-oriented tool results are printed one
   row per line for that reason). Filesystem tools are never evicted (they are the way back).
3. ``SubAgentMiddleware`` (advisor only).
4. deepagents ``SummarizationMiddleware`` (ours replaces the default) - when system + messages
   + tool schemas exceed ``summarization_trigger_tokens`` (approximate count, never a model
   call) the older messages are summarized by ``summarization_model`` (or the agent's own),
   the evicted history is offloaded to ``/conversation_history/<session>.md`` on the
   backend, and the *request* becomes ``[summary HumanMessage, last K messages]``. State
   (``result["messages"]``) is left untouched; the event is kept in private state.
5. ``PatchToolCallsMiddleware`` (deepagents default).
6. ``ContextAuditMiddleware`` (nvplan.ai.audit; new entry, spliced after the core) - sees the
   request after 4.
7. ``AnthropicPromptCachingMiddleware`` (ours replaces the tail default) - cache breakpoints
   on the system prompt and last tool; no-op for non-Anthropic models.

Deliberately absent: ``ContextEditingMiddleware(ClearToolUsesEdit)``. Clearing blanks old
tool results in the request; if a data-bearing result (actuals, parameters, plan-vs-actual,
notes) is cleared the model may cite figures from memory. Eviction is safe because it moves
the big result to a file and leaves a pointer, so the model can ``read_file`` it back.

Backend: ``CompositeBackend(default=StateBackend(), routes={"/skills/": FilesystemBackend(
root_dir=nvplan/ai/skills, virtual_mode=True)})``. Offloads (``/conversation_history/``,
``/large_tool_results/``) land in graph state (``result["files"]``) - never on disk, never in
the DB - while ``/skills/<name>/SKILL.md`` is read from the repo. ``CompositeBackend`` strips
the route prefix, so the route root is the ``skills`` directory itself.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Sequence

from langchain_core.language_models import BaseChatModel

from nvplan import config

SKILLS_DIR = Path(__file__).resolve().parent / "skills"
SKILLS_SOURCE = "/skills/"
#: Every skill directory under ``skills/``. Production loads the whole directory via
#: ``SKILLS_SOURCE``; this tuple is the manifest tests check the tree against, so a skill
#: added on disk without being named here is caught rather than silently shipped.
SKILL_NAMES: tuple[str, ...] = (
    # the three AI touchpoints
    "env-scan-54-positions",
    "revenue-proposal-method",
    "deviation-explanation-method",
    # the strategy and positioning cluster (PLATFORM.md P3)
    "strategy-lean-canvas",
    "strategy-monetization",
    "strategy-porters-five-forces",
    "strategy-positioning",
    "strategy-product-vision",
    "strategy-swot",
    # the conversational assistant (UI.md Part 3)
    "assistant-citation-method",
    # drafting a decision/hypothesis from a question (PLATFORM.md §12.6, work package B3)
    "decision-drafting-method",
)

#: Which surface each skill is FOR - the one real mapping every ``skills=`` consumer builds its
#: source from (``skills_for_surface`` / ``make_backend`` / ``make_shared_touchpoint_backend``
#: below), rather than each builder reaching for the whole directory. Mirrored by
#: ``tests/test_reachability.py``'s ``INTENDED_SKILL_SURFACE``, which asserts against this dict
#: directly instead of duplicating it as separate, driftable data.
SKILL_SURFACE: dict[str, str] = {
    "env-scan-54-positions": "env_scan",
    "revenue-proposal-method": "revenue_proposal",
    "deviation-explanation-method": "deviation_explanation",
    "strategy-lean-canvas": "assistant",
    "strategy-monetization": "assistant",
    "strategy-porters-five-forces": "assistant",
    "strategy-positioning": "assistant",
    "strategy-product-vision": "assistant",
    "strategy-swot": "assistant",
    "assistant-citation-method": "assistant",
    "decision-drafting-method": "assistant",
}

#: Every surface named in SKILL_SURFACE, once each.
SURFACES: tuple[str, ...] = tuple(sorted(set(SKILL_SURFACE.values())))


def skills_for_surface(surface: str) -> tuple[str, ...]:
    """Every skill name owned by ``surface`` (SKILL_SURFACE), in SKILL_NAMES order."""
    if surface not in SURFACES:
        raise ValueError(f"unknown surface {surface!r}; expected one of {SURFACES}")
    return tuple(name for name in SKILL_NAMES if SKILL_SURFACE[name] == surface)


def skills_index_route(surface: str) -> str:
    """The scoped ``skills=`` SOURCE for one surface's index ONLY - ``"/skills-index/<surface>/"``
    - distinct from the canonical, unscoped ``SKILLS_SOURCE`` ("/skills/") that every touchpoint's
    authored system prompt already hardcodes as ``read_file("/skills/<skill>/SKILL.md")``
    (``nvplan.ai.prompts``) and that ``nvplan.ai.fake.SKILL_PATHS`` scripts against. Used only by
    ``touchpoint_subagents``/``make_shared_touchpoint_backend``, where several subagents share one
    backend (see their own docstrings for why a distinct *route* per surface, not a distinct
    backend, is the only lever available there)."""
    if surface not in SURFACES:
        raise ValueError(f"unknown surface {surface!r}; expected one of {SURFACES}")
    return f"/skills-index/{surface}/"


READ_ONLY_FS_TOOLS: list[str] = ["read_file", "ls", "grep"]


@dataclass(frozen=True)
class ContextPolicy:
    """Thresholds for the context middleware. All token figures are approximate
    (``count_tokens_approximately``: chars/4 + 3 per message)."""

    summarization_trigger_tokens: int = config.AI_CONTEXT_SUMMARIZE_AT
    summarization_keep_messages: int = config.AI_CONTEXT_SUMMARIZE_KEEP_MESSAGES
    tool_result_evict_tokens: int = config.AI_CONTEXT_TOOL_RESULT_EVICT_TOKENS
    cache_ttl: str = config.AI_CONTEXT_CACHE_TTL
    summarization_model: BaseChatModel | None = None  # None -> the agent's own model

    def __post_init__(self) -> None:
        if self.cache_ttl not in ("5m", "1h"):
            raise ValueError(f"cache_ttl must be '5m' or '1h', got {self.cache_ttl!r}")
        for name in ("summarization_trigger_tokens", "tool_result_evict_tokens"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")


DEFAULT_POLICY = ContextPolicy()


class _ScopedSkillsBackend:
    """Wraps a real backend, restricting what ``ls``/``als`` ever LIST to a named allow-list of
    directory names - the only filter this closes.

    Why a wrapper and not a deepagents argument: checked against the installed deepagents
    (0.7.13) - ``create_deep_agent(skills=...)``, a ``SubAgent``'s own ``"skills"`` key, and
    ``SkillsMiddleware(sources=...)`` itself all accept only SOURCE PATHS (a directory whose
    immediate ``ls()`` children are the skill directories - see
    ``deepagents/middleware/skills.py``'s own docstring, "Sources point to skill directories in
    the backend"). None of them takes a skill NAME or an allow-list; several sources can be
    passed, but each one is still a whole directory, and every skill on disk here shares exactly
    one parent (``SKILLS_DIR``), so there is no combination of real source paths that names a
    proper subset of siblings. This is the closest correct thing given that constraint: a normal
    ``BackendProtocol`` implementer (the same pattern ``CompositeBackend``/``StateBackend``/
    ``FilesystemBackend`` already are), not a new argument anywhere in deepagents.

    Every other call (``download_files``, ``read_file``, ``grep``, ...) is untouched - delegated
    to the wrapped backend via ``__getattr__`` - because by the time one of those is called, the
    path in question already came from an ``ls()`` this wrapper already pruned."""

    def __init__(self, backend: Any, allowed: Sequence[str]) -> None:
        self._backend = backend
        self._allowed = frozenset(allowed)

    def _prune(self, result: Any) -> Any:
        if result.entries is None:
            return result
        entries = [
            e for e in result.entries if not e.get("is_dir") or Path(e["path"].rstrip("/")).name in self._allowed
        ]
        return replace(result, entries=entries)

    def ls(self, path: str) -> Any:
        return self._prune(self._backend.ls(path))

    async def als(self, path: str) -> Any:
        return self._prune(await self._backend.als(path))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._backend, name)


def make_backend(surface: str | None = None, skills_dir: Path | None = None) -> Any:
    """In-memory state for everything; the repo's skills directory read-only under
    ``SKILLS_SOURCE`` ("/skills/") - the whole directory when ``surface`` is omitted (unchanged
    default, still what a caller with no per-surface concept of its own gets), or pruned to just
    ``surface``'s own skills (``skills_for_surface`` / ``_ScopedSkillsBackend``) when given. The
    route path itself never changes, so the literal ``"/skills/<skill>/SKILL.md"`` paths
    ``nvplan.ai.prompts`` already hardcodes keep resolving unchanged either way - only which
    skills the agent's own INDEX (and its own ``ls`` tool) ever lists is pruned.

    Used by the STANDALONE builders (``build_touchpoint_agent``, ``build_assistant_agent``),
    each of which owns its own backend instance - see ``make_shared_touchpoint_backend`` for the
    advisor's subagents, which share one backend and need a different lever."""
    from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend

    fs = FilesystemBackend(root_dir=skills_dir or SKILLS_DIR, virtual_mode=True)
    skills_backend: Any = fs if surface is None else _ScopedSkillsBackend(fs, skills_for_surface(surface))
    return CompositeBackend(default=StateBackend(), routes={SKILLS_SOURCE: skills_backend})


def make_shared_touchpoint_backend(surfaces: Sequence[str], skills_dir: Path | None = None) -> Any:
    """The advisor's own backend, shared with every touchpoint subagent it builds
    (``touchpoint_subagents``): the canonical ``SKILLS_SOURCE`` route stays the WHOLE, unfiltered
    directory (every subagent's authored system prompt hardcodes a literal
    ``"/skills/<skill>/SKILL.md"`` ``read_file`` path - ``nvplan.ai.prompts`` - and that has to
    keep resolving no matter which subagent is running), plus one additional, distinct
    ``skills_index_route(surface)`` per surface in ``surfaces``, each pruned to just that
    surface's own skills.

    Why the canonical route can't itself be pruned per subagent here, unlike the standalone
    builders' ``make_backend(surface=...)``: deepagents' subagent construction hands every
    declarative ``SubAgent``'s ``SkillsMiddleware`` the SAME outer ``backend=`` the parent agent
    was given (``deepagents/graph.py``'s subagent-building loop: `SkillsMiddleware(backend=backend,
    sources=subagent_skills)`, always the one outer ``backend`` - never a per-subagent one). One
    shared backend object can register a name only once, so scoping has to live at a distinct
    *route* per surface instead - each subagent's own ``spec["skills"]`` names its own route
    (``skills_index_route``), pruning only what THAT subagent's own "Skills System" index lists;
    the shared, unpruned canonical route is what keeps every subagent's literal
    ``read_file("/skills/<skill>/SKILL.md")`` call working regardless of which one is running."""
    from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend

    fs = FilesystemBackend(root_dir=skills_dir or SKILLS_DIR, virtual_mode=True)
    routes: dict[str, Any] = {SKILLS_SOURCE: fs}
    for surface in surfaces:
        routes[skills_index_route(surface)] = _ScopedSkillsBackend(fs, skills_for_surface(surface))
    return CompositeBackend(default=StateBackend(), routes=routes)


def build_context_middleware(policy: ContextPolicy | None, *, model: BaseChatModel, backend: Any) -> list[Any]:
    """The context stack for one agent, in the order handed to ``create_deep_agent``.

    Filesystem, Summarization and PromptCaching replace deepagents' defaults by name (see the
    module doc for the compiled order and for why there is no tool-result clearing).
    """
    from deepagents.middleware.filesystem import FilesystemMiddleware
    from deepagents.middleware.summarization import SummarizationMiddleware
    from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware
    from langchain_core.messages.utils import count_tokens_approximately

    policy = policy or DEFAULT_POLICY
    return [
        FilesystemMiddleware(
            backend=backend,
            tools=list(READ_ONLY_FS_TOOLS),
            tool_token_limit_before_evict=policy.tool_result_evict_tokens,
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
