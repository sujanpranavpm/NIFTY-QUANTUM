"""Data-integrity auditing.

A strategy result is only as good as the bars underneath it, so every symbol is
audited before it reaches the engine and the audit is exported with the results.
The auditor separates *hard* failures (unusable data -> symbol excluded) from
*soft* flags (reported, not excluded).
"""

from __future__ import annotations

import pandas as pd

from nifty_quant.data.calendar import TradingCalendar
from nifty_quant.data.schema import FrameQuality, validate_ohlc
from nifty_quant.utils import get_logger

LOG = get_logger("nifty_quant.quality")

# An adjusted daily series should essentially never gap 40% in one session.
EXTREME_GAP_THRESHOLD = 0.40


class DataIntegrityError(RuntimeError):
    """Raised when the dataset cannot support a meaningful backtest."""


def audit_frame(
    symbol: str,
    frame: pd.DataFrame,
    calendar: TradingCalendar | None = None,
) -> FrameQuality:
    """Compute quality diagnostics for one symbol."""
    if frame is None or frame.empty:
        return FrameQuality(symbol=symbol, rows=0, start="", end="", problems=[f"{symbol}: no data"])

    problems = validate_ohlc(frame, symbol=symbol)
    returns = frame["close"].pct_change()
    quality = FrameQuality(
        symbol=symbol,
        rows=int(len(frame)),
        start=frame.index[0].date().isoformat(),
        end=frame.index[-1].date().isoformat(),
        missing_ohlc_rows=int(frame[["open", "high", "low", "close"]].isna().any(axis=1).sum()),
        zero_volume_rows=int((frame["volume"].fillna(0) <= 0).sum()),
        stale_close_rows=int((returns.fillna(1.0) == 0).sum()),
        extreme_gap_rows=int((returns.abs() > EXTREME_GAP_THRESHOLD).sum()),
        missing_sessions=calendar.missing_sessions(frame) if calendar is not None else 0,
        problems=problems,
    )
    return quality


def audit_universe(
    frames: dict[str, pd.DataFrame],
    calendar: TradingCalendar | None = None,
) -> pd.DataFrame:
    """Audit every symbol and return a tidy CSV-ready table."""
    rows = [audit_frame(symbol, frame, calendar).as_row() for symbol, frame in sorted(frames.items())]
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def assert_frames_usable(
    frames: dict[str, pd.DataFrame],
    min_rows: int = 250,
    raise_on_error: bool = True,
) -> list[str]:
    """Check that enough clean data exists to run a study.

    Returns the list of symbols rejected for hard problems. With
    ``raise_on_error`` the caller is stopped instead of silently reporting
    results built on broken bars.
    """
    rejected: list[str] = []
    for symbol, frame in frames.items():
        problems = validate_ohlc(frame, symbol) if frame is not None else [f"{symbol}: missing"]
        if len(frame) < min_rows or problems:
            rejected.append(symbol)
            LOG.warning("%s rejected: %d rows, problems=%s", symbol, 0 if frame is None else len(frame), problems)
    if rejected and raise_on_error:
        raise DataIntegrityError(
            f"{len(rejected)} symbol(s) failed integrity checks: {rejected[:5]}"
            + (" ..." if len(rejected) > 5 else "")
        )
    return rejected
