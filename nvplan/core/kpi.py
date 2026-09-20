"""The KPI catalogue: ratios derived from the statements.

A category is a sum (P&L line, balance-sheet position, cash-flow line); a KPI is a ratio, and
the ratio is the form a controller actually reads. The planning engine (regression, projector,
statements) produces the first; this module turns a year's worth of them into the second.

Definitions are **declarative**, not code. Each KPI states which statement lines it sums into a
numerator and a denominator and which operation combines them. Three consequences follow, and
all three are the point:

* the formula shown on screen is rendered from the same fields the evaluator reads
  (:meth:`KpiDefinition.formula`), so displayed text cannot drift from computed arithmetic;
* adding a KPI is a data change (one more :class:`KpiDefinition`), not a new function;
* nothing is hidden behind an ``eval()`` or a lambda nobody can inspect.

Thresholds are **assumptions**, not measurements. They are carried on the definition and
reported alongside every value (:attr:`Thresholds.source`) so a reader sees the standard being
applied and that it needs the client's confirmation, rather than presenting it as fact.

The eleven KPIs below are curated, not exhaustive: every numerator/denominator name is a line
code this repo's own statement chain (:mod:`nvplan.core.statements`) actually produces for every
scenario/year, plus one derived figure (``investment``, the period's capex -- the cash-flow
frame only carries its sign-flipped ``investing_cf``, so it is derived once, declared in
:data:`DERIVED_INPUTS`, rather than every KPI needing to know the sign convention). Nothing here
is aspirational: a metric this repo cannot compute from figures it already produces is not in
this catalogue.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

DAYS_IN_YEAR = 365


class Quadrant(str, Enum):
    """The controlling areas a KPI belongs to -- finance, liquidity, the customer relationship,
    or the workforce. Grouping by quadrant is what turns eleven numbers into a scorecard rather
    than a list."""

    FINANCE = "finance"
    LIQUIDITY = "liquidity"
    CUSTOMER = "customer"
    PERSONNEL = "personnel"


class Operation(str, Enum):
    """How a KPI combines its inputs."""

    RATIO = "ratio"
    """numerator / denominator -- a margin or a share."""

    DIFFERENCE = "difference"
    """numerator - denominator -- an absolute position such as working capital."""

    DAYS = "days"
    """numerator / denominator * 365 -- a duration such as days sales outstanding."""


class Direction(str, Enum):
    """Which way is good. Without this a traffic light cannot be coloured."""

    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"


class Status(str, Enum):
    """The traffic light. ``UNKNOWN`` where no threshold has been agreed, or the value itself
    is undefined (a zero denominator) -- deliberately not green either way."""

    GREEN = "green"
    AMBER = "amber"
    RED = "red"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Thresholds:
    """Bounds for the traffic light.

    ``good`` and ``warn`` are read in the direction the KPI improves: for a higher-is-better
    KPI, at or above ``good`` is green and below ``warn`` is red. Both boundaries are inclusive
    on the good/amber side -- see :func:`grade`.
    """

    good: float
    warn: float
    source: str = "assumption -- requires client confirmation, not a measured standard"


@dataclass(frozen=True, slots=True)
class KpiDefinition:
    """One KPI, declared rather than coded."""

    code: str
    name: str
    quadrant: Quadrant
    operation: Operation
    numerator: tuple[str, ...]
    denominator: tuple[str, ...]
    unit: str
    direction: Direction
    description: str
    thresholds: Thresholds | None = None

    def formula(self) -> str:
        """The formula as it will be evaluated, for display.

        Rendered from the same fields :func:`evaluate` reads, so the text on screen cannot
        drift from the arithmetic behind it.
        """
        top = " + ".join(self.numerator)
        bottom = " + ".join(self.denominator)
        if self.operation is Operation.RATIO:
            return f"({top}) / ({bottom})"
        if self.operation is Operation.DIFFERENCE:
            return f"({top}) - ({bottom})"
        return f"({top}) / ({bottom}) * {DAYS_IN_YEAR}"


@dataclass(frozen=True, slots=True)
class KpiValue:
    """A computed KPI, inseparable from how it was computed."""

    definition: KpiDefinition
    year: int
    value: float | None
    inputs: Mapping[str, float]
    status: Status

    @property
    def is_computable(self) -> bool:
        return self.value is not None


class MissingInputError(KeyError):
    """Raised when a definition names an input the caller did not supply."""

    def __init__(self, code: str, missing: Sequence[str]) -> None:
        super().__init__(
            f"KPI {code!r} needs inputs that were not supplied: {', '.join(sorted(missing))}. "
            "A KPI computed from a silently-defaulted zero would be wrong rather than missing."
        )
        self.missing = tuple(missing)


#: Statement line codes a KPI numerator/denominator may name (nvplan.core.statements.PL_LINES /
#: BS_LINES / CF_LINES, minus the codes no KPI below needs), plus ``investment`` -- the one
#: figure the statement chain does not carry directly (the CF frame only has the sign-flipped
#: ``investing_cf``). Declared once so :func:`inputs_from_statements` cannot silently supply a
#: name no `KpiDefinition` reads and no definition can silently name a figure nothing computes.
STATEMENT_INPUTS: tuple[str, ...] = (
    "revenue", "total_costs", "ebit", "personnel", "external",
    "cash", "receivables", "payables", "equity", "total_assets",
    "operating_cf",
)
DERIVED_INPUTS: tuple[str, ...] = ("investment",)


CATALOGUE: tuple[KpiDefinition, ...] = (
    KpiDefinition(
        code="ebit_margin",
        name="EBIT Margin",
        quadrant=Quadrant.FINANCE,
        operation=Operation.RATIO,
        numerator=("ebit",),
        denominator=("revenue",),
        unit="%",
        direction=Direction.HIGHER_IS_BETTER,
        description="Operating result (EBIT) as a share of revenue.",
        thresholds=Thresholds(good=0.08, warn=0.03),
    ),
    KpiDefinition(
        code="cost_ratio",
        name="Cost Ratio",
        quadrant=Quadrant.FINANCE,
        operation=Operation.RATIO,
        numerator=("total_costs",),
        denominator=("revenue",),
        unit="%",
        direction=Direction.LOWER_IS_BETTER,
        description="Total costs as a share of revenue.",
        thresholds=Thresholds(good=0.92, warn=0.97),
    ),
    KpiDefinition(
        code="equity_ratio",
        name="Equity Ratio",
        quadrant=Quadrant.FINANCE,
        operation=Operation.RATIO,
        numerator=("equity",),
        denominator=("total_assets",),
        unit="%",
        direction=Direction.HIGHER_IS_BETTER,
        description="Equity as a share of total assets.",
        thresholds=Thresholds(good=0.30, warn=0.15),
    ),
    KpiDefinition(
        code="liquidity_first_degree",
        name="Liquidity (Cash Ratio)",
        quadrant=Quadrant.LIQUIDITY,
        operation=Operation.RATIO,
        numerator=("cash",),
        denominator=("payables",),
        unit="%",
        direction=Direction.HIGHER_IS_BETTER,
        description="Cash against short-term liabilities.",
        thresholds=Thresholds(good=0.20, warn=0.10),
    ),
    KpiDefinition(
        code="liquidity_second_degree",
        name="Liquidity (Quick Ratio)",
        quadrant=Quadrant.LIQUIDITY,
        operation=Operation.RATIO,
        numerator=("cash", "receivables"),
        denominator=("payables",),
        unit="%",
        direction=Direction.HIGHER_IS_BETTER,
        description="Cash plus receivables against short-term liabilities.",
        thresholds=Thresholds(good=1.20, warn=1.00),
    ),
    KpiDefinition(
        code="net_working_capital",
        name="Net Working Capital",
        quadrant=Quadrant.LIQUIDITY,
        operation=Operation.DIFFERENCE,
        numerator=("receivables", "cash"),
        denominator=("payables",),
        unit="k EUR",
        direction=Direction.HIGHER_IS_BETTER,
        description="Short-term assets less short-term liabilities.",
    ),
    KpiDefinition(
        code="free_cash_flow",
        name="Free Cash Flow",
        quadrant=Quadrant.LIQUIDITY,
        operation=Operation.DIFFERENCE,
        numerator=("operating_cf",),
        denominator=("investment",),
        unit="k EUR",
        direction=Direction.HIGHER_IS_BETTER,
        description="Operating cash flow after investment (capex).",
    ),
    KpiDefinition(
        code="days_sales_outstanding",
        name="Days Sales Outstanding",
        quadrant=Quadrant.CUSTOMER,
        operation=Operation.DAYS,
        numerator=("receivables",),
        denominator=("revenue",),
        unit="days",
        direction=Direction.LOWER_IS_BETTER,
        description="Average days between invoicing and payment (receivables / revenue * 365).",
        thresholds=Thresholds(good=45.0, warn=70.0),
    ),
    KpiDefinition(
        code="personnel_cost_ratio",
        name="Personnel Cost Ratio",
        quadrant=Quadrant.PERSONNEL,
        operation=Operation.RATIO,
        numerator=("personnel",),
        denominator=("total_costs",),
        unit="%",
        direction=Direction.LOWER_IS_BETTER,
        description="Personnel as a share of total costs.",
        thresholds=Thresholds(good=0.65, warn=0.75),
    ),
    KpiDefinition(
        code="revenue_per_personnel_euro",
        name="Revenue per Personnel Euro",
        quadrant=Quadrant.PERSONNEL,
        operation=Operation.RATIO,
        numerator=("revenue",),
        denominator=("personnel",),
        unit="x",
        direction=Direction.HIGHER_IS_BETTER,
        description="Revenue generated per euro of personnel cost.",
        thresholds=Thresholds(good=1.60, warn=1.35),
    ),
    KpiDefinition(
        code="external_services_ratio",
        name="External Services Ratio",
        quadrant=Quadrant.PERSONNEL,
        operation=Operation.RATIO,
        numerator=("external",),
        denominator=("revenue",),
        unit="%",
        direction=Direction.LOWER_IS_BETTER,
        description="Subcontracted delivery (external services) as a share of revenue.",
        thresholds=Thresholds(good=0.15, warn=0.25),
    ),
)
"""Curated and human-authored, not generated. Every entry names only figures
:data:`STATEMENT_INPUTS` / :data:`DERIVED_INPUTS` supply -- ``test_kpi.py`` asserts that, so a
KPI naming a figure nothing computes cannot silently ship."""


def find(code: str) -> KpiDefinition:
    """Look a definition up by code.

    Raises:
        KeyError: If the code is not in the catalogue.
    """
    for definition in CATALOGUE:
        if definition.code == code:
            return definition
    msg = f"{code!r} is not in the KPI catalogue."
    raise KeyError(msg)


def evaluate(definition: KpiDefinition, inputs: Mapping[str, float], year: int) -> KpiValue:
    """Compute one KPI for one year.

    A division by zero yields ``None`` rather than an exception or an infinity: the KPI is
    genuinely undefined there, and reporting it as missing is the truthful answer. Missing
    inputs, by contrast, raise -- a KPI computed from a silently-defaulted zero would be wrong
    rather than absent, which is worse.

    Raises:
        MissingInputError: If an input the definition names was not supplied.
    """
    required = {*definition.numerator, *definition.denominator}
    missing = required - set(inputs)
    if missing:
        raise MissingInputError(definition.code, sorted(missing))

    used = {name: inputs[name] for name in sorted(required)}
    top = sum(inputs[name] for name in definition.numerator)
    bottom = sum(inputs[name] for name in definition.denominator)

    value: float | None
    if definition.operation is Operation.DIFFERENCE:
        value = top - bottom
    elif bottom == 0:
        value = None
    elif definition.operation is Operation.RATIO:
        value = top / bottom
    else:
        value = top / bottom * DAYS_IN_YEAR

    return KpiValue(definition=definition, year=year, value=value, inputs=used, status=grade(definition, value))


def grade(definition: KpiDefinition, value: float | None) -> Status:
    """Colour the traffic light.

    ``UNKNOWN`` when the value is undefined or no threshold has been agreed -- deliberately not
    green. An unmeasured KPI showing green is the failure this whole system is built to avoid.

    **Both boundaries are inclusive.** Landing exactly on the good threshold is green, and
    exactly on the warn threshold is amber -- the convention a reader assumes when told "green
    at 10% or better", and the one that does not need explaining when a figure lands on the line.
    """
    if value is None or definition.thresholds is None:
        return Status.UNKNOWN

    good, warn = definition.thresholds.good, definition.thresholds.warn
    if definition.direction is Direction.HIGHER_IS_BETTER:
        if value >= good:
            return Status.GREEN
        return Status.AMBER if value >= warn else Status.RED
    if value <= good:
        return Status.GREEN
    return Status.AMBER if value <= warn else Status.RED


def evaluate_all(inputs: Mapping[str, float], year: int) -> list[KpiValue]:
    """Compute every KPI whose inputs are available for a year.

    A definition whose inputs are missing is skipped rather than raising, so a
    partially-populated year still yields the KPIs it can support.
    """
    values: list[KpiValue] = []
    for definition in CATALOGUE:
        try:
            values.append(evaluate(definition, inputs, year))
        except MissingInputError:
            continue
    return values
