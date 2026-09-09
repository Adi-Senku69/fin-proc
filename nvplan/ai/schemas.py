"""Structured-output schemas for the three AI touchpoints.

Each touchpoint agent is built with ``response_format=ToolStrategy(<schema>)``
so the final model turn is a validated object, not free text. The object is
serialised into ``ai_record.response_text`` next to the model's prose.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RevenueProposal(BaseModel):
    """Touchpoint 2: one proposed revenue value for one plan year."""

    year: int = Field(description="Target plan year the proposal applies to.")
    proposed_value: float = Field(description="Proposed revenue for that year, in k EUR.")
    rationale: str = Field(description="Written rationale: which factors, what arithmetic, why this value.")
    cited_note_ids: list[int] = Field(default_factory=list, description="external_note ids that drive the proposal.")
    flagged_factors: list[str] = Field(default_factory=list, description="Short labels of the external factors used.")


class Contribution(BaseModel):
    category_code: str
    plan: float
    actual: float
    deviation: float = Field(description="actual - plan, in k EUR.")
    explanation: str = Field(description="Attribution of the deviation, citing the figures numerically.")


class DeviationExplanation(BaseModel):
    """Touchpoint 3: plan-vs-actual explanation for one scenario and year."""

    scenario: str
    year: int
    summary: str
    contributions: list[Contribution] = Field(default_factory=list)


class FlaggedPosition(BaseModel):
    domain: str
    position: str
    materiality: Literal["low", "medium", "high"]
    reasoning: str
    source: str = Field(description='Where the finding comes from; "assumption/illustrative" when not sourced.')


class EnvScanResult(BaseModel):
    """Touchpoint 1: material positions of the environmental framework."""

    flagged: list[FlaggedPosition] = Field(default_factory=list)
    summary: str


TOUCHPOINT_SCHEMAS: dict[str, type[BaseModel]] = {
    "env_scan": EnvScanResult,
    "revenue_proposal": RevenueProposal,
    "deviation_explanation": DeviationExplanation,
}
