"""Pydantic request / response models of the API (what the UI would consume)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- misc


class HealthOut(BaseModel):
    status: str = "ok"
    db_url: str
    illustrative: bool
    n_actuals: int
    n_scenarios: int


class IngestOut(BaseModel):
    rows: int
    source_label: str
    source: str
    illustrative: bool
    notes_seeded: int = 0


# --------------------------------------------------------------------------- plan


class PlanRunIn(BaseModel):
    created_by: str = Field(min_length=1)
    label_suffix: str = ""


class PlanRunOut(BaseModel):
    scenario_ids: dict[str, int]
    parameter_ids: dict[str, int]
    n_derivations: int
    n_plan_values: int
    n_statement_lines: int
    label: str
    created_at: str | None


class GridCell(BaseModel):
    value: float
    path: str
    plan_value_id: int
    ai_record_id: int | None = None


class GridRow(BaseModel):
    category_code: str
    name: str
    kind: str
    is_component: bool
    cells: dict[int, GridCell]


class ParameterRow(BaseModel):
    category_code: str
    parameter_id: int | None = None
    alpha: float | None = None
    beta: float | None = None
    r_squared: float | None = None
    valorization_rate: float | None = None
    growth_rate: float | None = None  # REV: g of the valorized default path
    window_from: int | None = None
    window_to: int | None = None
    calc_version: str | None = None


class PlanGridOut(BaseModel):
    scenario_id: int
    scenario_kind: str
    scenario_label: str
    created_by: str
    created_at: str | None
    illustrative: bool
    unit: str = "k EUR"
    years: list[int]
    rows: list[GridRow]
    parameters: list[ParameterRow]


class ScenarioOut(BaseModel):
    id: int
    kind: str
    label: str
    created_by: str
    created_at: str | None


# --------------------------------------------------------------------------- statements


class StatementCell(BaseModel):
    value: float
    statement_line_id: int
    mapping_ref: str


class StatementRow(BaseModel):
    line_code: str
    cells: dict[int, StatementCell]


class StatementGridOut(BaseModel):
    scenario_id: int
    scenario_kind: str
    scenario_label: str
    statement: str
    illustrative: bool
    unit: str = "k EUR"
    years: list[int]
    rows: list[StatementRow]
    consistency: list[str] = Field(default_factory=list, description="BS balance / cash-tie problems; empty = clean")


# --------------------------------------------------------------------------- ai


class AiRecordOut(BaseModel):
    id: int
    touchpoint: str
    status: str
    model_version: str
    proposed_value: float | None
    scenario_id: int | None
    category_code: str | None
    year: int | None
    rationale: str
    prompt_text: str
    response_text: str
    confirmed_by: str | None
    confirmed_at: str | None
    created_at: str | None
    note_ids: list[int] = Field(default_factory=list, description="external notes written by this record (env scan)")


class AiRecordDetailOut(AiRecordOut):
    """One record incl. the per-model-call audit log (GET /ai/records/{id} only; the list stays light)."""

    call_log: list[dict[str, Any]] = Field(
        default_factory=list,
        description="One entry per model call: the literal system prompt + messages sent, the response, "
        "approx tokens, and summarization / eviction / cleared-tool-result markers.",
    )


class ConfirmIn(BaseModel):
    confirmed_by: str = Field(min_length=1)


class RejectIn(BaseModel):
    rejected_by: str = Field(min_length=1)


class ConfirmOut(BaseModel):
    record: AiRecordOut
    run: PlanRunOut | None


class RevenueProposalIn(BaseModel):
    scenario_kind: str = "base"
    year: int


class EnvScanIn(BaseModel):
    positions_subset: list[str] | None = None


class DeviationExplanationIn(BaseModel):
    scenario_kind: str = "base"
    year: int


class RevenueProposalOut(BaseModel):
    record: AiRecordOut
    default_value: float


# --------------------------------------------------------------------------- analysis


class BacktestOut(BaseModel):
    n_cases: int
    thresholds: dict[str, float]
    summary: list[dict[str, Any]]
    summary_default_path: list[dict[str, Any]]
    fits: list[dict[str, Any]]
    markdown: str
    illustrative: bool


class DeviationOut(BaseModel):
    scenario_id: int
    scenario_kind: str
    scenario_label: str
    years: list[int]
    rows: list[dict[str, Any]]
    note: str


class BacktestYearOut(BaseModel):
    year: int
    train_window: list[int]
    rows: list[dict[str, Any]]
    fits: list[dict[str, Any]]
    note: str
