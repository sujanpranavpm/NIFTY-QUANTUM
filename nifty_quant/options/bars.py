"""Turning one contract's candles into an :class:`OptionBar` series with a
timestamp-anchored entry, preserving the engine's next-bar discipline.

The engine enters at ``bars[signal_index + 1].open``.  A daily signal raised at the
close of session *D* must therefore fill on session *D+1*'s first premium bar, and
the mapping is done on **timestamps** (the ``trading_day`` of each candle), never by
indexing a daily-signal list into a bar list.  Concretely:

* ``entry_index`` is the first bar whose ``trading_day`` equals the entry session;
* ``signal_index`` is ``entry_index - 1`` (a real earlier bar, so the engine's
  next-bar rule lands exactly on the entry session);
* if the contract has no bar on the entry session there is no entry to make
  (``NO_ENTRY_SESSION_BARS``); if its first-ever bar *is* the entry session there is
  no anchor (``ANCHOR_MISSING``) - in both cases nothing is fabricated.

Expiry is marked on the session's final bar so a still-open trade settles at that
close (``EXPIRY_SETTLE``).  When a contract did not print on its expiry session the
series ends with an unobservable ``is_expiry`` sentinel, which the engine turns into
``UNRESOLVED`` rather than inventing a settlement price.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import pandas as pd

from nifty_quant.engine.options_execution import OptionBar


@dataclass
class BarSeries:
    bars: list[OptionBar] = field(default_factory=list)
    signal_index: int = -1
    entry_index: int = -1
    ok: bool = False
    reason: Optional[str] = None
    entry_open: Optional[float] = None

    @property
    def n_bars(self) -> int:
        return len(self.bars)


def _timestamp(trading_day: str, candle_open) -> pd.Timestamp:
    clock = candle_open.time() if hasattr(candle_open, "time") else "09:15:00"
    return pd.Timestamp(f"{str(trading_day)[:10]} {clock}")


def build_option_bars(contract_bars: pd.DataFrame, *, strike: float, option_type: str,
                      entry_day: date, expiry: date) -> BarSeries:
    """Build the ordered :class:`OptionBar` series and the timestamp entry anchor.

    ``contract_bars`` is the whole expiry candle file (all strikes/types/days); the
    rows are filtered to one contract, ordered by ``(trading_day, candle)``, and
    truncated at expiry.
    """
    if contract_bars is None or not len(contract_bars):
        return BarSeries(reason="NO_CONTRACT_DATA")

    df = contract_bars
    mask = (df["option_type"] == option_type) & (df["strike"].astype(float) == float(strike))
    sub = df[mask].copy()
    expiry_str = str(expiry)[:10]
    sub = sub[sub["trading_day"].astype(str).str[:10] <= expiry_str]
    if not len(sub):
        return BarSeries(reason="NO_CONTRACT_DATA")

    sub = sub.sort_values(["trading_day", "candle"], kind="mergesort")

    bars: list[OptionBar] = []
    rows = list(sub.itertuples(index=False))
    for i, row in enumerate(rows):
        day = str(row.trading_day)[:10]
        ts = _timestamp(day, row.candle_open)
        # the final bar of the expiry session carries the settlement marker
        is_expiry = (day == expiry_str) and (
            i == len(rows) - 1 or str(rows[i + 1].trading_day)[:10] != expiry_str)
        bars.append(OptionBar(
            timestamp=ts,
            open=float(row.open), high=float(row.high),
            low=float(row.low), close=float(row.close),
            volume=float(row.volume), available=True, is_expiry=bool(is_expiry)))

    # If the contract never printed on its expiry session, append an unobservable
    # expiry sentinel so a trade still open at expiry is UNRESOLVED, not settled at a
    # price we do not have.
    has_expiry_session = any(str(r.trading_day)[:10] == expiry_str for r in rows)
    if not has_expiry_session:
        bars.append(OptionBar(
            timestamp=pd.Timestamp(f"{expiry_str} 15:15:00"),
            open=0.0, high=0.0, low=0.0, close=0.0,
            volume=0.0, available=False, is_expiry=True))

    entry_str = str(entry_day)[:10]
    entry_positions = [i for i, b in enumerate(bars)
                       if str(pd.Timestamp(b.timestamp).date()) == entry_str]
    if not entry_positions:
        return BarSeries(bars=bars, reason="NO_ENTRY_SESSION_BARS")
    entry_index = entry_positions[0]
    if entry_index == 0:
        return BarSeries(bars=bars, reason="ANCHOR_MISSING")

    signal_index = entry_index - 1
    entry_bar = bars[entry_index]
    if not entry_bar.available:
        return BarSeries(bars=bars, reason="NO_ENTRY_SESSION_BARS")

    return BarSeries(bars=bars, signal_index=signal_index, entry_index=entry_index,
                     ok=True, entry_open=float(entry_bar.open))
