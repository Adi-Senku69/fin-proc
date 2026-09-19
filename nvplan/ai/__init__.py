"""AI advisory layer (deepagents): three read-only touchpoints plus the conversational
assistant (UI.md Part 3), see README.md."""

from nvplan.ai.agents import (
    MissingCredentials,
    ModelRefused,
    ProposalRejected,
    build_advisor,
    build_chat_model,
    build_touchpoint_agent,
    credential_hint,
    credentials_available,
    get_model,
    model_version,
    run_deviation_explanation,
    run_env_scan,
    run_revenue_proposal,
)
from nvplan.ai.assistant import (
    AnswerRejected,
    AssistantAnswer,
    ClaimSegment,
    FigureRef,
    FigureSegment,
    Proposal,
    TextSegment,
    ask,
    build_assistant_agent,
    loose_figures,
    verify_answer,
)
from nvplan.ai.audit import ContextAuditMiddleware, total_usage
from nvplan.ai.context import DEFAULT_POLICY, ContextPolicy, build_context_middleware
from nvplan.ai.figures import ExplanationRejected, check_explanation, plan_vs_actual
from nvplan.ai.tools import AiRunContext, make_read_tools, make_write_tools

__all__ = [
    "AiRunContext",
    "AnswerRejected",
    "AssistantAnswer",
    "ClaimSegment",
    "ContextAuditMiddleware",
    "FigureRef",
    "FigureSegment",
    "MissingCredentials",
    "ModelRefused",
    "Proposal",
    "TextSegment",
    "ask",
    "build_assistant_agent",
    "build_chat_model",
    "credential_hint",
    "credentials_available",
    "loose_figures",
    "model_version",
    "total_usage",
    "verify_answer",
    "ContextPolicy",
    "DEFAULT_POLICY",
    "ExplanationRejected",
    "check_explanation",
    "plan_vs_actual",
    "build_context_middleware",
    "ProposalRejected",
    "build_advisor",
    "build_touchpoint_agent",
    "get_model",
    "make_read_tools",
    "make_write_tools",
    "run_deviation_explanation",
    "run_env_scan",
    "run_revenue_proposal",
]
