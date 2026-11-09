"""Live market-data engine (P2.2.10.2): provider adapters, collection, validation.

The package is provider-independent by construction.  ``nifty_quant.market_data.
providers.base`` defines the interface every provider implements; ``upstox`` is the
first real implementation.  Nothing in the research, options or backtesting layers
imports a concrete provider: they consume normalised ticks and locally aggregated
candles, exactly as they already consume the offline ingestion artifacts.

Data flow (see ``P2.2.10.2_UPSTOX_LIVE_MARKET_DATA_ENGINE_REPORT.md``)::

    provider REST  ->  instrument universe  ->  WebSocket feed
                                                     |
                          raw frames (JSONL, append-only)
                                                     |
                          normalise  ->  validate  ->  ticks (Parquet)
                                                     |
                          1m buckets -> 5m / 15m candles (Parquet)
                                                     |
                          ingestion registry (staging + provenance)  ->  research

Nothing here places, modifies or cancels an order, and nothing authenticates to a
broker beyond a read-only market-data token.
"""

from __future__ import annotations

__all__ = [
    "MarketDataConfig",
    "AnalyticsToken",
    "MarketDataService",
    "ConnectionState",
    "SubscriptionMode",
    "InstrumentUniverse",
]

from nifty_quant.market_data.config import AnalyticsToken, MarketDataConfig
from nifty_quant.market_data.providers.base import ConnectionState, SubscriptionMode
from nifty_quant.market_data.instruments import InstrumentUniverse
from nifty_quant.market_data.service import MarketDataService
