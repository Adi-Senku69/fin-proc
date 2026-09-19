"""AI advisory layer (deepagents): three read-only touchpoints, see README.md."""

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
from nvplan.ai.audit import ContextAuditMiddleware, total_usage
from nvplan.ai.context import DEFAULT_POLICY, ContextPolicy, build_context_middleware
from nvplan.ai.figures import ExplanationRejected, check_explanation, plan_vs_actual
from nvplan.ai.tools import AiRunContext, make_read_tools, make_write_tools

__all__ = [
    "AiRunContext",
    "ContextAuditMiddleware",
    "MissingCredentials",
    "ModelRefused",
    "build_chat_model",
    "credential_hint",
    "credentials_available",
    "model_version",
    "total_usage",
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
