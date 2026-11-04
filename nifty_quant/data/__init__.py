"""Market-data layer: providers, cache, universe, calendar, integrity checks."""

from __future__ import annotations

from nifty_quant.data.cache import BarStore, CacheEntry
from nifty_quant.data.calendar import TradingCalendar
from nifty_quant.data.providers.base import DataProvider, ProviderCapability
from nifty_quant.data.providers.csv_provider import CSVProvider
from nifty_quant.data.providers.yfinance_provider import YFinanceProvider
from nifty_quant.data.quality import DataIntegrityError, audit_frame, audit_universe
from nifty_quant.data.schema import (
    SchemaError,
    apply_corporate_actions,
    normalise_bars,
    validate_ohlc,
)
from nifty_quant.data.universe import Constituent, Universe, refresh_snapshot

__all__ = [
    "BarStore",
    "CacheEntry",
    "CSVProvider",
    "Constituent",
    "DataIntegrityError",
    "DataProvider",
    "ProviderCapability",
    "SchemaError",
    "TradingCalendar",
    "Universe",
    "YFinanceProvider",
    "apply_corporate_actions",
    "audit_frame",
    "audit_universe",
    "normalise_bars",
    "refresh_snapshot",
    "validate_ohlc",
]
