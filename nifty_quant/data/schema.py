"""Canonical bar-frame schema, validation and corporate-action adjustment."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import numpy as np
import pandas as pd

CANONICAL_COLUMNS = ("open", "high", "low", "close", "adj_close", "volume")
OPTIONAL_COLUMNS = ("dividends", "stock_splits", "raw_close")
REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")


class SchemaError(ValueError):
    """Raised when a bar frame cannot be used for backtesting."""


def normalise_bars(df: pd.DataFrame, symbol: str = "?") -> pd.DataFrame:
    """Coerce a provider frame into the canonical lowercase schema.

    Accepts the yfinance column names (``Open``, ``Adj Close``, ...) as well as
    already-normalised frames. The index is converted to tz-naive timestamps
    localised to the exchange calendar (Asia/Kolkata for NSE).
    """
    if df is None or len(df) == 0:
        raise SchemaError(f"{symbol}: empty frame")

    frame = df.copy()
    if isinstance(frame.columns, pd.MultiIndex):
        # yfinance returns (field, ticker) pairs when a batch download has one ticker.
        frame.columns = [str(col[0]) for col in frame.columns]

    rename = {
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Adj Close": "adj_close",
        "AdjClose": "adj_close",
        "Volume": "volume",
        "Dividends": "dividends",
        "Stock Splits": "stock_splits",
    }
    frame = frame.rename(columns={c: rename.get(str(c), str(c).lower().replace(" ", "_")) for c in frame.columns})

    missing = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
    if missing:
        raise SchemaError(f"{symbol}: missing required columns {missing}")

    if "adj_close" not in frame.columns:
        frame["adj_close"] = frame["close"]

    index = pd.DatetimeIndex(frame.index)
    if index.tz is not None:
        index = index.tz_convert("Asia/Kolkata").tz_localize(None)
    frame.index = index
    frame.index.name = "timestamp"

    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    for col in CANONICAL_COLUMNS:
        frame[col] = pd.to_numeric(frame[col], errors="coerce").astype(float)
    for col in OPTIONAL_COLUMNS:
        if col in frame.columns:
            frame[col] = pd.to_numeric(frame[col], errors="coerce").astype(float)

    keep = list(CANONICAL_COLUMNS) + [c for c in OPTIONAL_COLUMNS if c in frame.columns]
    return frame[keep]


def apply_corporate_actions(frame: pd.DataFrame) -> pd.DataFrame:
    """Back-adjust OHLC for splits, bonuses and dividends.

    The adjustment factor is ``adj_close / close`` from the provider, which is
    the standard total-return factor. Applying it to every OHLC field prevents
    an unadjusted bonus/split from appearing as a 50% "breakout" or as a stop
    that was never really hit.

    Raw prices are preserved in ``raw_close`` for audit. The factor uses
    dividends declared *after* the historical bar, which is the conventional
    back-adjustment and is documented as a known methodological limitation.
    """
    out = frame.copy()
    close = out["close"].replace(0.0, np.nan)
    factor = (out["adj_close"] / close).astype(float)
    factor = factor.replace([np.inf, -np.inf], np.nan).ffill().fillna(1.0)

    out["raw_close"] = out["close"]
    for col in ("open", "high", "low", "close"):
        out[col] = out[col] * factor
    return out


def validate_ohlc(frame: pd.DataFrame, symbol: str = "?") -> list[str]:
    """Return a list of hard integrity violations (empty when the frame is sound)."""
    problems: list[str] = []
    if frame.empty:
        return [f"{symbol}: empty frame"]
    if frame.index.has_duplicates:
        problems.append(f"{symbol}: duplicate timestamps")
    if not frame.index.is_monotonic_increasing:
        problems.append(f"{symbol}: index not monotonically increasing")

    if frame[list(REQUIRED_COLUMNS)].isna().any().any():
        bad = int(frame[list(REQUIRED_COLUMNS)].isna().any(axis=1).sum())
        problems.append(f"{symbol}: {bad} rows with NaN in OHLCV")

    valid = frame.dropna(subset=["open", "high", "low", "close"])
    if (valid[["open", "high", "low", "close"]] <= 0).any().any():
        problems.append(f"{symbol}: non-positive prices")
    if (valid["high"] < valid["low"]).any():
        problems.append(f"{symbol}: high < low")
    if (valid["high"] + 1e-9 < valid[["open", "close"]].max(axis=1)).any():
        problems.append(f"{symbol}: high below open/close")
    if (valid["low"] - 1e-9 > valid[["open", "close"]].min(axis=1)).any():
        problems.append(f"{symbol}: low above open/close")
    if (valid["volume"] < 0).any():
        problems.append(f"{symbol}: negative volume")
    return problems


@dataclass
class FrameQuality:
    """Diagnostics for one symbol, suitable for CSV export."""

    symbol: str
    rows: int
    start: str
    end: str
    missing_ohlc_rows: int = 0
    zero_volume_rows: int = 0
    stale_close_rows: int = 0
    extreme_gap_rows: int = 0
    missing_sessions: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def is_usable(self) -> bool:
        return self.rows > 0 and not self.problems

    def as_row(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "rows": self.rows,
            "start": self.start,
            "end": self.end,
            "missing_ohlc_rows": self.missing_ohlc_rows,
            "zero_volume_rows": self.zero_volume_rows,
            "stale_close_rows": self.stale_close_rows,
            "extreme_gap_rows": self.extreme_gap_rows,
            "missing_sessions": self.missing_sessions,
            "hard_problems": "; ".join(self.problems),
            "usable": self.is_usable,
        }


def incomplete_sessions(
    frame: pd.DataFrame, calendar: Iterable[pd.Timestamp] | None = None
) -> int:
    """Count exchange sessions with no bar.

    When ``calendar`` is None the frame's own span is used, which only detects
    internal holes (weekends/holidays are not distinguishable without a
    reference calendar).
    """
    if frame.empty:
        return 0
    if calendar is not None:
        reference = pd.DatetimeIndex(sorted(set(pd.DatetimeIndex(calendar))))
        span = reference[(reference >= frame.index.min()) & (reference <= frame.index.max())]
        return int(len(span.difference(frame.index)))
    span = pd.date_range(frame.index.min(), frame.index.max(), freq="B")
    return int(len(pd.DatetimeIndex(span).difference(frame.index)))
