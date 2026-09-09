"""Depreciation schedule reproduces the generator's DEPR actuals exactly."""

from __future__ import annotations

import pandas as pd
import pytest

from nvplan.config import DATA_DIR
from nvplan.core import DerivationLedger, to_wide
from nvplan.core.depreciation import depreciation_schedule


@pytest.fixture(scope="module")
def investment_plan() -> pd.DataFrame:
    return pd.read_csv(DATA_DIR / "investment_plan.csv")


@pytest.fixture(scope="module")
def depr_actuals() -> pd.Series:
    wide = to_wide(pd.read_csv(DATA_DIR / "actuals.csv"))
    return wide["DEPR"]


def test_schedule_matches_actuals_2016_2025(investment_plan, depr_actuals):
    ledger = DerivationLedger()
    sched = depreciation_schedule(investment_plan[["year", "capex"]], life_years=5, ledger=ledger)
    assert list(sched.columns) == ["year", "capex", "depreciation", "nbv"]
    assert sched["year"].tolist() == list(range(2016, 2031))
    by_year = sched.set_index("year")["depreciation"]
    for year in range(2016, 2026):
        assert by_year.loc[year] == pytest.approx(depr_actuals.loc[year], abs=1e-6), year
    # and the full 2016-2030 column the generator wrote
    for year, expected in zip(investment_plan["year"], investment_plan["depreciation"]):
        assert by_year.loc[year] == pytest.approx(expected, abs=1e-6), year


def test_nbv_is_cumulative_capex_minus_cumulative_depreciation(investment_plan):
    sched = depreciation_schedule(investment_plan, ledger=DerivationLedger())
    cum = (sched["capex"].cumsum() - sched["depreciation"].cumsum()).tolist()
    assert sched["nbv"].tolist() == pytest.approx(cum, abs=1e-9)
    # generator's net_book_value for 2025 (opening fixed assets in bs_mapping.yaml)
    upto = investment_plan[investment_plan["year"] <= 2025]
    assert sched.set_index("year").loc[2025, "nbv"] == pytest.approx(
        upto["capex"].sum() - upto["depreciation"].sum(), abs=1e-6
    )


def test_derivations_list_contributing_tranches(investment_plan):
    ledger = DerivationLedger()
    sched = depreciation_schedule(investment_plan, ledger=ledger)
    ledger.validate()
    d2016 = ledger.get("depr:2016")
    assert [t["capex_year"] for t in d2016.inputs["tranches"]] == [2016]
    assert d2016.inputs["tranches"][0]["charge"] == pytest.approx(319.7 / 5)
    assert d2016.parents == ("capex:2016",)
    d2021 = ledger.get("depr:2021")
    assert [t["capex_year"] for t in d2021.inputs["tranches"]] == [2017, 2018, 2019, 2020, 2021]
    assert sum(t["charge"] for t in d2021.inputs["tranches"]) == pytest.approx(
        sched.set_index("year").loc[2021, "depreciation"]
    )
    assert d2021.parents == tuple(f"capex:{y}" for y in range(2017, 2022))
    assert d2021.parameters["life_years"] == 5
    assert ledger.get("capex:2021").inputs["capex"] == 450.8
    order = ledger.topo_order()
    assert all(order.index(p) < order.index("depr:2021") for p in d2021.parents)


def test_straight_line_by_hand():
    plan = pd.DataFrame({"year": [2020, 2021, 2022], "capex": [100.0, 0.0, 50.0]})
    sched = depreciation_schedule(plan, life_years=2, ledger=DerivationLedger())
    assert sched["depreciation"].tolist() == pytest.approx([50.0, 50.0, 25.0])
    assert sched["nbv"].tolist() == pytest.approx([50.0, 0.0, 25.0])
    with pytest.raises(ValueError):
        depreciation_schedule(plan, life_years=0, ledger=DerivationLedger())
    with pytest.raises(ValueError):
        depreciation_schedule(pd.DataFrame({"year": [2020, 2020], "capex": [1.0, 2.0]}), ledger=DerivationLedger())
