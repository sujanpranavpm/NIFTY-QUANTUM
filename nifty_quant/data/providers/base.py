"""Data-provider abstraction.

A provider's only job is to return a canonical bar frame for one symbol and
interval. Caching, adjustment, validation and calendar handling live outside the
provider so that a new source (NSE bhavcopy archive, paid vendor, broker API)
can be dropped in without touching the engine.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import pandas as pd

SUPPORTED_INTERVALS = ("1d", "1wk", "5m", "15m", "30m", "60m")


@dataclass(frozen=True)
class ProviderCapability:
    """What a provider can and cannot serve, stated explicitly."""

    name: str
    daily: bool = True
    intraday_intervals: tuple[str, ...] = ()
    max_intraday_history_days: int | None = None
    corporate_actions: bool = False
    adjusted_prices: bool = False
    survivorship_free_universe: bool = False
    notes: str = ""


class DataProvider(ABC):
    """Interface implemented by every market-data source."""

    capability: ProviderCapability

    @abstractmethod
    def fetch(
        self,
        symbol: str,
        interval: str = "1d",
        start: str | None = None,
        end: str | None = None,
    ) -> pd.DataFrame:
        """Return canonical OHLCV bars, or raise if the symbol cannot be served.

        The returned frame must be normalised (lowercase columns, tz-naive index)
        and must cover only completed bars. ``end`` is exclusive.
        """

    def fetch_many(
        self,
        symbols: list[str],
        interval: str = "1d",
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, pd.DataFrame]:
        """Default batch implementation: sequential fetch, skips failures."""
        out: dict[str, pd.DataFrame] = {}
        for symbol in symbols:
            try:
                out[symbol] = self.fetch(symbol, interval, start, end)
            except Exception as exc:  # noqa: BLE001 - provider failures are per symbol
                out[symbol] = pd.DataFrame()
                self.last_errors[symbol] = str(exc)
        return out

    @property
    def last_errors(self) -> dict[str, str]:
        if not hasattr(self, "_last_errors"):
            self._last_errors = {}
        return self._last_errors

    def supports(self, interval: str) -> bool:
        if interval == "1d":
            return self.capability.daily
        return interval in self.capability.intraday_intervals
