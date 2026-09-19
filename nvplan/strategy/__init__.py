"""Strategy and positioning capability cluster (PLATFORM.md §8, Phase P3).

The one part of this cluster that is code rather than a skill: market sizing, because two
competent people given the same population, penetration, price and shares should produce the
same TAM/SAM/SOM. Everything else in the cluster (vision, canvas, SWOT, five forces, positioning,
monetization narrative) is a runtime skill under ``nvplan/ai/skills/`` - its value is judgment,
and its output is claims that carry a provenance tag and land in ``brain/`` as a decision or a
hypothesis, never bare prose.
"""

from nvplan.strategy.sizing import (
    SizingInput,
    SizingResult,
    market_size,
    sam_key,
    som_key,
    tam_key,
)

__all__ = [
    "SizingInput",
    "SizingResult",
    "market_size",
    "tam_key",
    "sam_key",
    "som_key",
]
