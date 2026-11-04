"""NIFTY 50 quantitative strategy research engine.

A faithful re-implementation of the 20 order-based strategies published in the
open ``quants.md`` / ``QuantJourneyOrg/quantjourney-bt`` strategy library, wired
into a research pipeline for NSE equities.

The package is deliberately split into layers:

``nifty_quant.data``        market-data providers, cache, universe, validation
``nifty_quant.engine``      order book, fill simulation, accounting, costs
``nifty_quant.strategies``  the 20 ported strategies + registry metadata
``nifty_quant.analytics``   trade extraction, metrics, ranking
``nifty_quant.validation``  IS/OOS, walk-forward, sensitivity, regimes, stats
``nifty_quant.reporting``   CSV exports and Plotly figures
``nifty_quant.signals``     research-only signal engine
``nifty_quant.api``         FastAPI service + persistence
``nifty_quant.dashboard``   Streamlit comparison dashboard

Nothing in this package places real orders.
"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__"]
