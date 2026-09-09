import json

import numpy as np
import pandas as pd
import pytest
import yaml

from nvplan.config import CATEGORY_CODES, DATA_DIR, ILLUSTRATIVE_LABEL
from nvplan.data import generate as gen

EXPECTED_FILES = [
    "actuals.csv",
    "actuals.xlsx",
    "investment_plan.csv",
    "true_parameters.json",
    "bs_mapping.yaml",
    "external_notes.csv",
    "env_framework.yaml",
    "control_table.yaml",
]


@pytest.fixture(scope="module")
def out_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("illustrative")
    gen.write_all(d)
    return d


def test_committed_files_exist():
    for name in EXPECTED_FILES:
        assert (DATA_DIR / name).exists(), name


def test_write_all_creates_files(out_dir):
    for name in EXPECTED_FILES:
        assert (out_dir / name).exists(), name


def test_deterministic(out_dir):
    a = pd.read_csv(out_dir / "actuals.csv")
    b = pd.read_csv(DATA_DIR / "actuals.csv")
    pd.testing.assert_frame_equal(a, b)
    assert gen.build_dataset()["series"]["REV"].tolist() == gen.build_dataset()["series"]["REV"].tolist()


def test_actuals_shape_and_label(out_dir):
    df = pd.read_csv(out_dir / "actuals.csv")
    assert set(df["category_code"]) == set(CATEGORY_CODES) | {"DEPR"}
    assert sorted(df["year"].unique()) == list(range(2016, 2026))
    assert len(df) == 10 * 6
    assert (df["source_label"] == ILLUSTRATIVE_LABEL).all()
    assert df.groupby("category_code")["year"].nunique().eq(10).all()


def test_personnel_share_of_costs(out_dir):
    df = pd.read_csv(out_dir / "actuals.csv").pivot(index="year", columns="category_code", values="value")
    total = df[["MAT", "EXT", "PERS", "OTH"]].sum(axis=1)  # OTH already includes DEPR
    share = df["PERS"] / total
    assert share.between(0.75, 0.85).all(), share.to_dict()


def test_revenue_growth(out_dir):
    df = pd.read_csv(out_dir / "actuals.csv")
    rev = df[df["category_code"] == "REV"].sort_values("year")["value"].to_numpy()
    assert 11_500 <= rev[0] <= 12_500
    assert (np.diff(rev) > 0).all()
    yoy = rev[1:] / rev[:-1] - 1
    assert (yoy > 0.03).all() and (yoy < 0.09).all()


def test_oth_includes_depreciation(out_dir):
    df = pd.read_csv(out_dir / "actuals.csv").pivot(index="year", columns="category_code", values="value")
    inv = pd.read_csv(out_dir / "investment_plan.csv").set_index("year")
    assert np.allclose(df["DEPR"], inv.loc[df.index, "depreciation"], atol=1e-3)
    assert (df["OTH"] > df["DEPR"]).all()


def test_investment_plan(out_dir):
    inv = pd.read_csv(out_dir / "investment_plan.csv")
    assert inv["year"].tolist() == list(range(2016, 2031))
    assert (inv["capex"] > 0).all()
    # straight-line 5 years starting in capex year: first year depreciation == capex/5
    assert inv.loc[0, "depreciation"] == pytest.approx(inv.loc[0, "capex"] / 5, abs=1e-3)


def test_true_parameters_json(out_dir):
    tp = json.loads((out_dir / "true_parameters.json").read_text())
    assert tp["seed"] == 42 and tp["label"] == ILLUSTRATIVE_LABEL
    assert set(tp["categories"]) == {"MAT", "EXT", "PERS", "OTH"}
    for code, p in tp["categories"].items():
        assert {"alpha", "v", "beta", "sigma", "realized_noise"} <= set(p)
        assert len(p["realized_noise"]) == 10
    # noise-free reconstruction + recorded noise reproduces the csv
    df = pd.read_csv(out_dir / "actuals.csv").pivot(index="year", columns="category_code", values="value")
    t = np.arange(10)
    for code, p in tp["categories"].items():
        clean = p["alpha"] * (1 + p["v"]) ** t + p["beta"] * df["REV"].to_numpy()
        expected = clean + np.array(p["realized_noise"])
        actual = df[code].to_numpy() - (df["DEPR"].to_numpy() if code == "OTH" else 0)
        assert np.allclose(actual, expected, atol=2e-3), code


def test_xlsx_matches_csv(out_dir):
    wide = pd.read_excel(out_dir / "actuals.xlsx", sheet_name="Istwerten").set_index("category_code")
    long = pd.read_csv(out_dir / "actuals.csv").set_index(["category_code", "year"])["value"]
    for code in wide.index:
        for year in range(2016, 2026):
            assert wide.loc[code, year] == pytest.approx(long.loc[(code, year)], abs=1e-6)


def test_env_framework_54(out_dir):
    fw = yaml.safe_load((out_dir / "env_framework.yaml").read_text())
    assert len(fw["domains"]) == 6
    assert all(len(d["positions"]) == 9 for d in fw["domains"])
    assert fw["position_count"] == 54


def test_control_table(out_dir):
    ct = yaml.safe_load((out_dir / "control_table.yaml").read_text())
    rp = ct["revenue_proposal"]
    assert rp["max_deviation_from_default_pct"] == 25
    assert rp["must_cite_note"] is True
    assert rp["scenario_spread"] == {"best": 0.08, "worst": -0.08}
    assert ct["tax_rate"] == 0.25


def test_bs_mapping_opening_balances(out_dir):
    bm = yaml.safe_load((out_dir / "bs_mapping.yaml").read_text())
    ob = bm["opening_balance_sheet"]
    assert ob["year"] == 2025
    assets = ob["cash"] + ob["receivables"] + ob["inventory"] + ob["fixed_assets"]
    assert assets == pytest.approx(ob["payables"] + ob["equity"], abs=1e-6)
    assert bm["label"] == ILLUSTRATIVE_LABEL


def test_external_notes(out_dir):
    notes = pd.read_csv(out_dir / "external_notes.csv")
    assert 3 <= len(notes) <= 4
    assert {"category_code", "year", "text", "author", "source"} <= set(notes.columns)
