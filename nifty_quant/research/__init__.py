"""Research orchestration, result aggregation and validation."""

from nifty_quant.research.pipeline import (
    MarketData,
    load_market_data,
    load_results,
    run_research,
    save_results,
)
from nifty_quant.research.results import ResearchResults

__all__ = [
    "MarketData",
    "ResearchResults",
    "load_market_data",
    "load_results",
    "run_research",
    "save_results",
]
