"""Daily index signals from the *unchanged* strategies.

A Tier-1 candidate is an order-mode equity strategy whose ``_compute_orders`` reads
indicator feature panels and goes long on a ``flat -> long`` transition (market,
bracket or stop entry).  Rather than reimplement that logic, the adapter runs the
strategy through the real engine on the **NIFTY index series treated as a single
instrument** and reads the resulting daily position.  A position that becomes long
on day *D* is the directional signal; the option is entered at *D+1* (the next
session), never on *D* - the same next-bar discipline the engine already pins.

Because every Tier-1 candidate is ``long_only``, a fresh long is a bullish
(``CE``) signal; there is no short leg, so these strategies never raise a bearish
(``PE``) signal on their own.  That is a property of the strategies, not the
adapter, and is recorded honestly rather than manufactured.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from nifty_quant.config import EngineConfig, ResearchConfig
from nifty_quant.engine.backtest import run_backtest

#: Capital used purely so an equity strategy sized as ``int(NAV*weight/close)`` takes a
#: non-zero position on a ~20,000-point index. It scales the *quantity* only - the
#: long/flat decision (and therefore the signal) is price-driven and unchanged.
_SIGNAL_CAPITAL = 10_000_000.0


@dataclass(frozen=True)
class DailySignal:
    """One directional signal raised at the close of ``day``."""

    strategy_id: str
    day: date
    direction: str            # "CE" (bullish) | "PE" (bearish)
    spot: float               # index close on ``day`` (reference for strike)
    event: str                # "flat_to_long" | "flat_to_short"


def _position_series(run) -> pd.Series:
    """The daily long/flat position of the single-instrument run."""
    if not run.slices:
        return pd.Series(dtype=float)
    equity = run.slices[0].equity
    if equity.empty or "positions" not in equity.columns:
        return pd.Series(dtype=float)
    return equity["positions"].astype(float)


def index_daily_signals(strategy_id: str, index_daily: pd.DataFrame,
                        *, direction_bias: str = "CE",
                        capital: float = _SIGNAL_CAPITAL) -> list[DailySignal]:
    """Run ``strategy_id`` on the index daily bars and return fresh-entry signals.

    ``index_daily`` must be a single-instrument OHLCV frame indexed by trading-day
    timestamp (the adapter builds it from the 1-minute index file).  A signal is a
    ``flat -> long`` transition; ``spot`` is that day's index close.  The strategy is
    executed unchanged through the real engine; only the capital scale (a quantity
    knob, never a decision knob) is set so an index-priced instrument can be held.
    """
    frame = index_daily.sort_index()
    if frame.empty:
        return []
    bars = {"NIFTY": frame}
    config = ResearchConfig(engine=EngineConfig(initial_capital=float(capital)))
    run = run_backtest(strategy_id, bars, config=config, interval="1d", mode="per_stock")

    positions = _position_series(run)
    closes = frame["close"]
    signals: list[DailySignal] = []
    prev = 0.0
    for stamp, pos in positions.items():
        long_now = float(pos) > 0.0 and prev <= 0.0
        if long_now:
            signals.append(DailySignal(
                strategy_id=strategy_id,
                day=stamp.date(),
                direction=direction_bias,
                spot=float(closes.loc[stamp]),
                event="flat_to_long",
            ))
        prev = float(pos)
    return signals
