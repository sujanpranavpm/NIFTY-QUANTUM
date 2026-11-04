"""Feature panel construction.

The upstream strategies read ``instruments_data.get_feature("SMA_20_close")``
style panels (dates x instruments). This module builds those panels once per run
so every strategy sees identical, already-shifted-correctly indicator values.

Every feature at row *t* is computed from bars up to and including *t* only. The
runner then ensures orders emitted at *t* cannot fill before *t+1*, so no feature
value and no fill can reference the future.
"""

from __future__ import annotations

from typing import Iterable, Mapping

import pandas as pd

from nifty_quant.indicators import atr, rsi, sma

BASE_COLUMNS = ("open", "high", "low", "close", "adj_close", "volume")


def _series(bars: pd.DataFrame, name: str) -> pd.Series | None:
    if name not in bars.columns:
        return None
    return bars[name].astype(float)


def requested_sma_periods(params: Mapping[str, object]) -> list[int]:
    periods = {20, 50}
    for key in ("fast", "slow", "sma_period"):
        value = params.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and int(value) > 1:
            periods.add(int(value))
    return sorted(periods)


def requested_rsi_periods(params: Mapping[str, object]) -> list[int]:
    periods = {14}
    value = params.get("rsi_period")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and int(value) > 1:
        periods.add(int(value))
    return sorted(periods)


def build_panels(
    bars: Mapping[str, pd.DataFrame],
    params: Mapping[str, object],
    *,
    rsi_method: str = "wilder",
    sma_periods: Iterable[int] | None = None,
    rsi_periods: Iterable[int] | None = None,
) -> dict[str, pd.DataFrame]:
    """Build ``{feature_name: DataFrame(index=dates, columns=symbols)}``."""
    sma_list = list(sma_periods) if sma_periods is not None else requested_sma_periods(params)
    rsi_list = list(rsi_periods) if rsi_periods is not None else requested_rsi_periods(params)
    if not bars:
        return {}

    per_symbol: dict[str, pd.DataFrame] = {}
    for symbol, frame in bars.items():
        if frame is None or frame.empty:
            continue
        close = _series(frame, "close")
        if close is None:
            raise KeyError(f"{symbol}: bars have no 'close' column")
        adj = _series(frame, "adj_close")
        adj = close if adj is None or adj.isna().all() else adj.fillna(close)

        columns: dict[str, pd.Series] = {}
        for name in BASE_COLUMNS:
            series = _series(frame, name)
            if series is not None:
                columns[name] = series
        columns["adj_close"] = adj

        for period in sma_list:
            columns[f"SMA_{period}_close"] = sma(close, period)
            columns[f"SMA_{period}_adj_close"] = sma(adj, period)
        for period in rsi_list:
            columns[f"RSI_{period}_close"] = rsi(close, period, rsi_method)

        high = _series(frame, "high")
        low = _series(frame, "low")
        if high is not None and low is not None:
            columns["ATR_20"] = atr(high, low, close, 20)

        per_symbol[symbol] = pd.DataFrame(columns)

    if not per_symbol:
        return {}

    names = sorted({name for frame in per_symbol.values() for name in frame.columns})
    panels: dict[str, pd.DataFrame] = {}
    for name in names:
        panels[name] = pd.DataFrame(
            {
                symbol: frame[name]
                for symbol, frame in per_symbol.items()
                if name in frame.columns
            }
        ).sort_index()
    return panels


def panel_index(bars: Mapping[str, pd.DataFrame]) -> pd.DatetimeIndex:
    """Union of all timestamps present in any instrument's bars."""
    indices = [frame.index for frame in bars.values() if frame is not None and len(frame)]
    if not indices:
        return pd.DatetimeIndex([])
    union = indices[0]
    for index in indices[1:]:
        union = union.union(index)
    return pd.DatetimeIndex(union).sort_values()


def trim_panels(
    panels: Mapping[str, pd.DataFrame], index: pd.DatetimeIndex
) -> dict[str, pd.DataFrame]:
    """Restrict every panel to the exact index the runner iterates over."""
    return {name: frame.reindex(index) for name, frame in panels.items()}
