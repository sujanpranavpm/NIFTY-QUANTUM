"""Offline / vendor-file provider.

Reads canonical CSVs from ``data/raw/<symbol>_<interval>.csv``. This is the
escape hatch for exchange-grade data (NSE bhavcopy archives, a paid vendor, a
broker export) without changing a line of engine code.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from nifty_quant.config import PATHS
from nifty_quant.data.providers.base import DataProvider, ProviderCapability
from nifty_quant.data.schema import normalise_bars


class CSVProvider(DataProvider):
    """Load bars from local CSV files."""

    capability = ProviderCapability(
        name="csv",
        daily=True,
        intraday_intervals=("5m", "15m", "30m", "60m"),
        corporate_actions=True,
        adjusted_prices=True,
        survivorship_free_universe=True,
        notes="Local files provided by the user; quality is whatever the file contains.",
    )

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root is not None else PATHS.raw
        self._last_errors: dict[str, str] = {}

    def path_for(self, symbol: str, interval: str) -> Path:
        return self.root / f"{symbol.replace('/', '_')}_{interval}.csv"

    def fetch(
        self,
        symbol: str,
        interval: str = "1d",
        start: str | None = None,
        end: str | None = None,
    ) -> pd.DataFrame:
        path = self.path_for(symbol, interval)
        if not path.exists():
            raise FileNotFoundError(f"no CSV for {symbol} {interval}: {path}")
        raw = pd.read_csv(path, index_col=0, parse_dates=True)
        frame = normalise_bars(raw, symbol=symbol)
        if start is not None:
            frame = frame[frame.index >= pd.Timestamp(start)]
        if end is not None:
            frame = frame[frame.index < pd.Timestamp(end)]
        if frame.empty:
            raise ValueError(f"{symbol} {interval}: CSV has no rows in the requested range")
        return frame
