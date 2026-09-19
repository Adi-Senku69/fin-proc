"""Phase P3 (PLATFORM.md §8, §10): the strategy and positioning capability cluster.

Part A - `nvplan/strategy/sizing.py` is code (arithmetic over stated assumptions) and every
figure it produces registers a derivation, exactly like `nvplan.core.regression`.

Part B - the six `strategy-*` skills under `nvplan/ai/skills/` are judgment, so each is tested
only for house-style shape: valid frontmatter, a description under the length cap, and body text
that states the provenance rule. This file does not depend on `nvplan.ai.context` (owned by
another agent mid-flight) - it reads the six new SKILL.md files directly, the way
`tests/test_ai_skills.py` reads the existing three.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from nvplan.core.ledger import DerivationLedger
from nvplan.strategy.sizing import (
    SizingInput,
    SizingResult,
    market_size,
    sam_key,
    som_key,
    tam_key,
)

SKILLS_DIR = Path(__file__).resolve().parent.parent / "nvplan" / "ai" / "skills"

NEW_SKILL_NAMES = [
    "strategy-product-vision",
    "strategy-lean-canvas",
    "strategy-swot",
    "strategy-porters-five-forces",
    "strategy-positioning",
    "strategy-monetization",
]

VALID_TAG = "(industry-knowledge)"


def _frontmatter(text: str) -> dict[str, str]:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert m, "SKILL.md must start with YAML frontmatter"
    return dict(line.split(":", 1) for line in m.group(1).splitlines() if ":" in line)


# --------------------------------------------------------------------------- Part A: sizing


@pytest.fixture()
def ledger() -> DerivationLedger:
    return DerivationLedger()


def _input(**overrides) -> SizingInput:
    kwargs = dict(
        population=1_000_000.0,
        penetration=0.10,
        price=0.05,  # kEUR per unit -> TAM = 1_000_000 * 0.10 * 0.05 = 5000 kEUR
        label="illustrative-segment",
        source_tag=VALID_TAG,
    )
    kwargs.update(overrides)
    return SizingInput(**kwargs)


def test_market_size_computes_by_hand(ledger):
    result = market_size(_input(), serviceable_share=0.4, obtainable_share=0.1, ledger=ledger)
    assert isinstance(result, SizingResult)
    assert result.tam == pytest.approx(5000.0)
    assert result.sam == pytest.approx(2000.0)  # 5000 * 0.4
    assert result.som == pytest.approx(200.0)  # 2000 * 0.1
    assert result.unit == "kEUR"
    assert result.derivation_keys == {
        "tam": tam_key("illustrative-segment"),
        "sam": sam_key("illustrative-segment"),
        "som": som_key("illustrative-segment"),
    }


def test_market_size_registers_derivations_with_lineage(ledger):
    result = market_size(_input(), serviceable_share=0.4, obtainable_share=0.1, ledger=ledger)
    k_tam, k_sam, k_som = (
        result.derivation_keys["tam"],
        result.derivation_keys["sam"],
        result.derivation_keys["som"],
    )

    d_tam = ledger.get(k_tam)
    assert d_tam.parents == ()
    assert "TAM" in d_tam.formula_text and "population" in d_tam.formula_text
    assert d_tam.inputs["population"] == 1_000_000.0
    assert d_tam.inputs["penetration"] == 0.10
    assert d_tam.inputs["price"] == 0.05
    assert d_tam.inputs["source_tag"] == VALID_TAG

    d_sam = ledger.get(k_sam)
    assert d_sam.parents == (k_tam,)
    assert d_sam.inputs["tam"] == pytest.approx(5000.0)
    assert d_sam.inputs["serviceable_share"] == 0.4

    d_som = ledger.get(k_som)
    assert d_som.parents == (k_sam,)
    assert d_som.inputs["sam"] == pytest.approx(2000.0)
    assert d_som.inputs["obtainable_share"] == 0.1

    # every parent resolves - the whole point of registering a derivation
    ledger.validate()
    order = ledger.topo_order()
    assert order.index(k_tam) < order.index(k_sam) < order.index(k_som)


@pytest.mark.parametrize(
    "bad_tag",
    [
        "",
        "just an assumption, no tag at all",
        "(stakeholder-verbal, Jane)",  # missing date
        "(intuition, PM)",  # missing date
        "(industry-knowledge) and also (chat, no artifact)",  # more than one tag
    ],
)
def test_missing_or_malformed_source_tag_is_rejected(bad_tag):
    with pytest.raises(ValueError):
        _input(source_tag=bad_tag)


@pytest.mark.parametrize("valid_tag", [
    "(industry-knowledge)",
    "(chat, no artifact)",
    "(stakeholder-verbal, Jane Doe, 2026-01-15)",
    "(intuition, PM, 2026-01-15)",
])
def test_every_enum_tag_form_is_accepted_as_a_source_tag(ledger, valid_tag):
    result = market_size(
        _input(source_tag=valid_tag, label=f"seg-{hash(valid_tag) & 0xffff}"),
        serviceable_share=0.4,
        obtainable_share=0.1,
        ledger=ledger,
    )
    assert result.tam == pytest.approx(5000.0)


@pytest.mark.parametrize(
    "share_kwargs",
    [
        {"serviceable_share": 0.0, "obtainable_share": 0.1},
        {"serviceable_share": -0.1, "obtainable_share": 0.1},
        {"serviceable_share": 1.5, "obtainable_share": 0.1},
        {"serviceable_share": 0.4, "obtainable_share": 0.0},
        {"serviceable_share": 0.4, "obtainable_share": 1.1},
    ],
)
def test_share_outside_zero_one_is_rejected(ledger, share_kwargs):
    with pytest.raises(ValueError):
        market_size(_input(), ledger=ledger, **share_kwargs)


def test_share_of_exactly_one_is_accepted(ledger):
    result = market_size(_input(), serviceable_share=1.0, obtainable_share=1.0, ledger=ledger)
    assert result.sam == pytest.approx(result.tam)
    assert result.som == pytest.approx(result.tam)


@pytest.mark.parametrize(
    "bad_kwargs",
    [
        {"population": 0.0},
        {"population": -5.0},
        {"price": 0.0},
        {"price": -1.0},
        {"penetration": 0.0},
        {"penetration": 1.5},
        {"label": ""},
        {"label": "   "},
    ],
)
def test_sizing_input_rejects_bad_assumptions(bad_kwargs):
    with pytest.raises(ValueError):
        _input(**bad_kwargs)


def test_duplicate_label_raises_on_reregistration(ledger):
    market_size(_input(), serviceable_share=0.4, obtainable_share=0.1, ledger=ledger)
    with pytest.raises(KeyError):
        market_size(_input(), serviceable_share=0.4, obtainable_share=0.1, ledger=ledger)


# --------------------------------------------------------------------------- Part B: skills


def test_all_six_skill_directories_exist():
    dirs = {p.name for p in SKILLS_DIR.iterdir() if p.is_dir()}
    for name in NEW_SKILL_NAMES:
        assert name in dirs
        assert (SKILLS_DIR / name / "SKILL.md").is_file()


@pytest.mark.parametrize("name", NEW_SKILL_NAMES)
def test_skill_has_valid_frontmatter(name):
    text = (SKILLS_DIR / name / "SKILL.md").read_text()
    fm = _frontmatter(text)
    assert fm["name"].strip() == name
    assert re.fullmatch(r"[a-z0-9-]{1,64}", name)
    description = fm["description"].strip()
    assert 1 <= len(description) <= 1024


@pytest.mark.parametrize("name", NEW_SKILL_NAMES)
def test_skill_states_the_provenance_rule(name):
    text = (SKILLS_DIR / name / "SKILL.md").read_text()
    assert "§4.1" in text
    assert "orphan" in text.lower()
    # the seven-form enum is spelled out, not just referenced
    assert "(industry-knowledge)" in text
    assert "(chat, no artifact)" in text
    assert "(computed, <derivation-key>)" in text


@pytest.mark.parametrize("name", NEW_SKILL_NAMES)
def test_skill_states_quantified_claims_come_from_code_not_invention(name):
    text = (SKILLS_DIR / name / "SKILL.md").read_text()
    assert "invent" in text.lower()
    assert "sizing.py" in text or "nvplan/strategy/sizing" in text
    assert "computed" in text.lower()


@pytest.mark.parametrize("name", NEW_SKILL_NAMES)
def test_skill_names_where_its_output_lands_in_the_brain(name):
    text = (SKILLS_DIR / name / "SKILL.md").read_text()
    assert "brain/decisions/" in text
    assert "## Evidence" in text
    assert "## Decision" in text
    assert "## What would reverse this" in text


def test_env_scan_reference_not_duplicated():
    # swot and porter's five forces explicitly point at env-scan-54-positions instead of
    # re-deriving the external-factor analysis.
    for name in ("strategy-swot", "strategy-porters-five-forces"):
        text = (SKILLS_DIR / name / "SKILL.md").read_text()
        assert "env-scan-54-positions" in text
        assert "duplicate" in text.lower()


def test_monetization_points_at_the_sizing_module():
    text = (SKILLS_DIR / "strategy-monetization" / "SKILL.md").read_text()
    assert "from nvplan.strategy.sizing import" in text
    assert "market_size" in text
