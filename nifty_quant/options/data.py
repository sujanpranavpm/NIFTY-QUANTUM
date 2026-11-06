"""Loading of the datasets the adapter runs on, from the on-disk archive.

Everything here is a *read* of data the research already owns (the cached P1
sample and the derived option candles).  No network, no fabrication: a missing
file or an unusable stub is reported, never back-filled.  The daily index series
and the intraday session ruler are derived from the same 1-minute index file so
the whole window (2021->2026) has one consistent session calendar - the official
bhavcopy calendar only reaches 2024+ and therefore cannot rule DTE earlier.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd

_EXPIRY_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _day(value) -> date:
    return date.fromisoformat(str(value)[:10])


class MarketData:
    """Read-only accessor over the NIFTY index + option candle files.

    Args:
        root: repository root (defaults to the package's grandparent).
        index_path: the 1-minute NIFTY index parquet.
        candles_dir: directory holding ``{resolution}__NIFTY__{expiry}.parquet``.
    """

    def __init__(
        self,
        root: Optional[Path] = None,
        index_path: Optional[Path] = None,
        candles_dir: Optional[Path] = None,
    ) -> None:
        self.root = Path(root) if root else Path(__file__).resolve().parents[2]
        self.index_path = Path(index_path) if index_path else (
            self.root / "data" / "raw" / "p1_sample" / "index__NIFTY.parquet")
        self.candles_dir = Path(candles_dir) if candles_dir else (
            self.root / "data" / "derived" / "candles")
        self._index_daily: Optional[pd.DataFrame] = None
        self._sessions: Optional[list[date]] = None
        self._option_cache: dict[tuple[str, str], pd.DataFrame] = {}

    # ---- daily index + session ruler --------------------------------------
    def index_daily(self) -> pd.DataFrame:
        """One row per index trading day: OHLCV, indexed by normalised timestamp.

        The close is the session's final 1-minute print (the 15:30 close under the
        open-labelled convention), so the daily bar reflects the true last trade.
        """
        if self._index_daily is not None:
            return self._index_daily
        raw = pd.read_parquet(self.index_path)
        raw = raw.sort_values("timestamp")
        agg = raw.groupby("trading_day").agg(
            open=("open", "first"), high=("high", "max"), low=("low", "min"),
            close=("close", "last"), volume=("volume", "sum"))
        agg.index = pd.to_datetime(agg.index).normalize()
        agg.index.name = "timestamp"
        self._index_daily = agg.sort_index().reset_index().set_index("timestamp")
        return self._index_daily

    def sessions(self) -> list[date]:
        """Sorted index trading sessions - the whole-window DTE ruler."""
        if self._sessions is None:
            self._sessions = sorted(_day(t) for t in self.index_daily().index)
        return self._sessions

    # ---- option candle files ----------------------------------------------
    def option_expiries(self, resolution: str = "15m") -> list[date]:
        """Every expiry date with a readable, non-empty candle file (ascending)."""
        out: list[date] = []
        for path in sorted(self.candles_dir.glob(f"{resolution}__NIFTY__*.parquet")):
            m = _EXPIRY_RE.search(path.stem)
            if not m:
                continue
            expd = _day(m.group(1))
            try:
                frame = self.option_frame(expd, resolution)
            except Exception:  # noqa: BLE001 - an empty-schema stub is unusable
                continue
            if len(frame):
                out.append(expd)
        return out

    def option_frame(self, expiry: date | str, resolution: str = "15m") -> pd.DataFrame:
        """The candles for one expiry file (all strikes, both CE/PE), cached."""
        key = (resolution, str(expiry)[:10])
        if key in self._option_cache:
            return self._option_cache[key]
        path = self.candles_dir / f"{resolution}__NIFTY__{key[1]}.parquet"
        frame = pd.read_parquet(path)
        if len(frame):
            frame = frame.sort_values(["trading_day", "candle"]).reset_index(drop=True)
        self._option_cache[key] = frame
        return frame

    # ---- contract lookup on a session -------------------------------------
    def contracts_on(self, expiry: date, day: date, resolution: str = "15m") -> pd.DataFrame:
        """Rows for one expiry whose trading_day == ``day`` (empty if the file is
        missing or the contract did not trade that session)."""
        try:
            frame = self.option_frame(expiry, resolution)
        except Exception:  # noqa: BLE001
            return frame_empty()
        if not len(frame) or "trading_day" not in frame.columns:
            return frame_empty()
        key = str(day)[:10]
        return frame[frame["trading_day"].astype(str).str[:10] == key]


def frame_empty() -> pd.DataFrame:
    return pd.DataFrame(columns=[
        "trading_day", "symbol", "expiry", "strike", "option_type", "candle",
        "open", "high", "low", "close", "volume", "open_interest", "candle_open"])
