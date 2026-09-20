"""Tests for the KPI catalogue and its evaluator (C2).

Two properties matter more than any individual ratio: a KPI is never computed from a silently
defaulted input, and a traffic light is never green without evidence behind it.
"""

from __future__ import annotations

import pytest

from nvplan.core.kpi import (
    CATALOGUE,
    DERIVED_INPUTS,
    STATEMENT_INPUTS,
    Direction,
    KpiDefinition,
    MissingInputError,
    Operation,
    Quadrant,
    Status,
    Thresholds,
    evaluate,
    evaluate_all,
    find,
    grade,
)

#: One year's worth of every figure the catalogue can name, in the units the statement chain
#: actually produces (k EUR). Values are picked so every ratio below has an unambiguous,
#: hand-checkable result.
INPUTS = {
    "revenue": 10_000.0,
    "total_costs": 9_200.0,
    "ebit": 800.0,
    "personnel": 6_000.0,
    "external": 1_200.0,
    "receivables": 1_500.0,
    "cash": 200.0,
    "payables": 900.0,
    "equity": 1_200.0,
    "total_assets": 4_000.0,
    "operating_cf": 500.0,
    "investment": 120.0,
}


class TestCatalogue:
    def test_every_definition_is_uniquely_coded(self):
        codes = [definition.code for definition in CATALOGUE]
        assert len(codes) == len(set(codes))

    def test_every_definition_is_computable_from_figures_the_engine_produces(self):
        """No aspirational entries: a catalogue listing KPIs this repo cannot produce would
        promise more than the system carries. Checked against the declared input surface
        (STATEMENT_INPUTS / DERIVED_INPUTS), not just against the local INPUTS fixture, so a
        definition cannot pass by accident of what this test file happened to include."""
        available = {*STATEMENT_INPUTS, *DERIVED_INPUTS}
        assert available <= set(INPUTS)  # the fixture above covers every declared input
        for definition in CATALOGUE:
            needed = {*definition.numerator, *definition.denominator}
            assert needed <= available, f"{definition.code} needs {needed - available}"

    def test_the_four_quadrants_are_represented(self):
        assert {definition.quadrant for definition in CATALOGUE} == set(Quadrant)

    def test_an_unknown_code_is_refused(self):
        with pytest.raises(KeyError, match="not in the KPI catalogue"):
            find("imaginary_kpi")

    def test_the_rendered_formula_matches_what_is_evaluated(self):
        """The text on screen must come from the same fields the evaluator reads."""
        liquidity = find("liquidity_second_degree")
        assert liquidity.formula() == "(cash + receivables) / (payables)"
        margin = find("ebit_margin")
        assert margin.formula() == "(ebit) / (revenue)"
        dso = find("days_sales_outstanding")
        assert dso.formula() == "(receivables) / (revenue) * 365"
        nwc = find("net_working_capital")
        assert nwc.formula() == "(receivables + cash) - (payables)"


class TestEvaluation:
    def test_ebit_margin(self):
        value = evaluate(find("ebit_margin"), INPUTS, 2027)
        assert value.value == pytest.approx(0.08)
        assert value.year == 2027

    def test_liquidity_second_degree_sums_its_numerator(self):
        # (200 + 1500) / 900 = 1.888...
        value = evaluate(find("liquidity_second_degree"), INPUTS, 2027)
        assert value.value == pytest.approx(1.8889, abs=0.0005)

    def test_days_sales_outstanding_converts_to_days(self):
        # 1500 / 10000 * 365 = 54.75
        value = evaluate(find("days_sales_outstanding"), INPUTS, 2027)
        assert value.value == pytest.approx(54.75)

    def test_net_working_capital_is_a_difference_not_a_ratio(self):
        # (1500 + 200) - 900 = 800
        value = evaluate(find("net_working_capital"), INPUTS, 2027)
        assert value.value == pytest.approx(800.0)

    def test_free_cash_flow_is_operating_cf_less_investment(self):
        # 500 - 120 = 380
        value = evaluate(find("free_cash_flow"), INPUTS, 2027)
        assert value.value == pytest.approx(380.0)

    def test_the_inputs_used_are_reported(self):
        """A value that cannot show its inputs cannot be defended."""
        value = evaluate(find("ebit_margin"), INPUTS, 2027)
        assert value.inputs == {"ebit": 800.0, "revenue": 10_000.0}

    def test_a_missing_input_raises_rather_than_defaulting(self):
        """A KPI computed from a silently defaulted zero is wrong, not missing."""
        with pytest.raises(MissingInputError) as excinfo:
            evaluate(find("ebit_margin"), {"revenue": 100.0}, 2027)
        assert "ebit" in str(excinfo.value)

    def test_a_zero_denominator_yields_no_value_rather_than_an_error(self):
        value = evaluate(find("ebit_margin"), {**INPUTS, "revenue": 0.0}, 2027)
        assert value.value is None
        assert not value.is_computable
        assert value.status is Status.UNKNOWN

    def test_evaluate_all_skips_what_it_cannot_compute(self):
        partial = {"revenue": 10_000.0, "ebit": 800.0}
        codes = {value.definition.code for value in evaluate_all(partial, 2027)}
        assert "ebit_margin" in codes
        assert "liquidity_first_degree" not in codes


class TestTrafficLight:
    def test_higher_is_better_grades_upward(self):
        definition = find("ebit_margin")
        assert grade(definition, 0.10) is Status.GREEN
        assert grade(definition, 0.05) is Status.AMBER
        assert grade(definition, 0.01) is Status.RED

    def test_lower_is_better_grades_downward(self):
        definition = find("personnel_cost_ratio")
        assert grade(definition, 0.60) is Status.GREEN
        assert grade(definition, 0.70) is Status.AMBER
        assert grade(definition, 0.82) is Status.RED

    def test_an_undefined_value_is_unknown_not_green(self):
        assert grade(find("ebit_margin"), None) is Status.UNKNOWN

    def test_a_kpi_without_agreed_thresholds_is_unknown_not_green(self):
        """An unmeasured KPI showing green is the failure this whole system is built to avoid."""
        definition = KpiDefinition(
            code="x", name="X", quadrant=Quadrant.FINANCE, operation=Operation.RATIO,
            numerator=("a",), denominator=("b",), unit="%", direction=Direction.HIGHER_IS_BETTER,
            description="", thresholds=None,
        )
        assert grade(definition, 999.0) is Status.UNKNOWN

    def test_thresholds_declare_where_they_came_from(self):
        """An assumption presented as a measurement would be the same error as inventing a
        figure -- ADR-style discipline this catalogue borrows from the reference brief."""
        for definition in CATALOGUE:
            if definition.thresholds is not None:
                assert "assumption" in definition.thresholds.source.lower()


def test_thresholds_are_ordered_in_the_direction_of_improvement():
    """A good bound the wrong side of the warn bound would invert every light."""
    for definition in CATALOGUE:
        if definition.thresholds is None:
            continue
        good, warn = definition.thresholds.good, definition.thresholds.warn
        if definition.direction is Direction.HIGHER_IS_BETTER:
            assert good > warn, definition.code
        else:
            assert good < warn, definition.code


class TestThresholdBoundaries:
    """Exactly on the line, in both directions -- an off-by-one in `grade` changes a colour on
    screen without changing a number, so this pins all four cases explicitly rather than relying
    on the catalogue's own thresholds to happen to exercise them."""

    @staticmethod
    def _definition(direction: Direction) -> KpiDefinition:
        good, warn = (0.10, 0.05) if direction is Direction.HIGHER_IS_BETTER else (0.05, 0.10)
        return KpiDefinition(
            code="boundary", name="Boundary", quadrant=Quadrant.FINANCE, operation=Operation.RATIO,
            numerator=("a",), denominator=("b",), unit="%", direction=direction,
            description="A KPI that exists to sit on its own thresholds.",
            thresholds=Thresholds(good=good, warn=warn),
        )

    def test_exactly_the_good_threshold_is_green_when_higher_is_better(self):
        assert grade(self._definition(Direction.HIGHER_IS_BETTER), 0.10) is Status.GREEN

    def test_exactly_the_warn_threshold_is_amber_when_higher_is_better(self):
        assert grade(self._definition(Direction.HIGHER_IS_BETTER), 0.05) is Status.AMBER

    def test_a_hair_below_the_warn_threshold_is_red_when_higher_is_better(self):
        assert grade(self._definition(Direction.HIGHER_IS_BETTER), 0.0499) is Status.RED

    def test_exactly_the_good_threshold_is_green_when_lower_is_better(self):
        assert grade(self._definition(Direction.LOWER_IS_BETTER), 0.05) is Status.GREEN

    def test_exactly_the_warn_threshold_is_amber_when_lower_is_better(self):
        assert grade(self._definition(Direction.LOWER_IS_BETTER), 0.10) is Status.AMBER

    def test_a_hair_above_the_warn_threshold_is_red_when_lower_is_better(self):
        assert grade(self._definition(Direction.LOWER_IS_BETTER), 0.1001) is Status.RED
