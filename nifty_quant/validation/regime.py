"""Market-regime conditioning.

A single full-period average hides the fact that trend systems earn in trends and
bleed in chop. This module labels every bar of the benchmark by trend direction
and volatility level, then re-expresses a strategy's results per regime so the
research can state *when* a strategy works rather than only *whether*.

Regimes are defined mechanically and are never used as an input to the trading
signals; they are an attribution lens applied after the fact.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TREND_BULL = "bull"
TREND_BEAR = "bear"
VOL_HIGH = "high_vol"
VOL_LOW = "low_vol"
UNKNOWN = "unknown"


def classify(
    benchmark: pd.DataFrame,
    *,
    ma_bars: int = 200,
    vol_window: int = 20,
) -> pd.DataFrame:
    """Label each benchmark bar with trend and volatility buckets.

    Args:
        benchmark: the index frame (``close`` required).
        ma_bars: trend filter length; ``close > SMA`` is a bull bar.
        vol_window: realised-volatility window.

    Returns a frame indexed like ``benchmark`` with ``trend``, ``vol`` and the
    combined ``regime`` label. Bars before the windows are warm are marked
    ``unknown`` rather than guessed.
    """
    close = benchmark["close"].astype(float)
    sma = close.rolling(ma_bars, min_periods=ma_bars).mean()
    returns = close.pct_change()
    realised = returns.rolling(vol_window, min_periods=vol_window).std()
    # Expanding median so the split cannot use future volatility levels.
    vol_line = realised.expanding(min_periods=vol_window * 4).median()

    trend = pd.Series(UNKNOWN, index=close.index, dtype=object)
    trend[close > sma] = TREND_BULL
    trend[close < sma] = TREND_BEAR

    vol = pd.Series(UNKNOWN, index=close.index, dtype=object)
    vol[realised > vol_line] = VOL_HIGH
    vol[realised <= vol_line] = VOL_LOW

    regime = pd.Series(UNKNOWN, index=close.index, dtype=object)
    known = (trend != UNKNOWN) & (vol != UNKNOWN)
    regime[known] = trend[known].str.cat(vol[known], sep="+")

    frame = pd.DataFrame({"trend": trend, "vol": vol, "regime": regime})
    frame["close"] = close
    frame["benchmark_return"] = returns
    return frame


def align_regimes(regimes: pd.DataFrame, index: pd.DatetimeIndex) -> pd.Series:
    """Regime label per bar of ``index``, carried forward from the last known label."""
    if regimes.empty:
        return pd.Series(UNKNOWN, index=index, dtype=object)
    labels = regimes["regime"].reindex(regimes.index.union(index)).ffill()
    return labels.reindex(index).fillna(UNKNOWN)


def regime_equity_stats(equity: pd.Series, labels: pd.Series) -> pd.DataFrame:
    """Per-regime return/vol/Sharpe attribution for one equity curve.

    Bars are grouped by the label in force at that bar, so a strategy that only
    trades in bulls shows almost all of its bars in the bull buckets.
    """
    if equity.empty:
        return pd.DataFrame()
    returns = equity.pct_change().dropna()
    if returns.empty:
        return pd.DataFrame()
    frame = pd.DataFrame({"ret": returns, "regime": labels.reindex(returns.index).fillna(UNKNOWN)})
    rows = []
    for label, group in frame.groupby("regime", sort=True):
        rets = group["ret"].astype(float)
        compounded = float((1.0 + rets).prod() - 1.0)
        sd = float(rets.std(ddof=1)) if len(rets) > 2 else float("nan")
        rows.append(
            {
                "regime": label,
                "bars": int(len(rets)),
                "share_of_bars": float(len(rets) / len(frame)),
                "compounded_return": compounded,
                "mean_bar_return": float(rets.mean()),
                "volatility": sd,
                "sharpe": (
                    float(rets.mean() / sd * np.sqrt(_ppy(returns.index)))
                    if sd and np.isfinite(sd) and sd > 0
                    else float("nan")
                ),
            }
        )
    return pd.DataFrame(rows).sort_values("regime").reset_index(drop=True)


def regime_trade_stats(trades: pd.DataFrame, labels: pd.Series) -> pd.DataFrame:
    """Per-regime closed-trade statistics (the trade's entry bar sets its bucket)."""
    if trades is None or trades.empty:
        return pd.DataFrame()
    closed = trades[~trades["is_open"]].copy()
    if closed.empty:
        return pd.DataFrame()
    closed["regime"] = labels.reindex(pd.DatetimeIndex(closed["entry_time"])).to_numpy()
    closed["regime"] = closed["regime"].fillna(UNKNOWN)
    rows = []
    for label, group in closed.groupby("regime", sort=True):
        pnl = group["net_pnl"].astype(float)
        wins = pnl[pnl > 0]
        losses = pnl[pnl < 0]
        rows.append(
            {
                "regime": label,
                "n_trades": int(len(pnl)),
                "win_rate": float(len(wins) / len(pnl)) if len(pnl) else float("nan"),
                "avg_pnl": float(pnl.mean()) if len(pnl) else float("nan"),
                "total_pnl": float(pnl.sum()),
                "profit_factor": (
                    float(wins.sum() / abs(losses.sum())) if losses.sum() < 0 else float("nan")
                ),
            }
        )
    return pd.DataFrame(rows).sort_values("regime").reset_index(drop=True)


def _ppy(index: pd.DatetimeIndex) -> float:
    from nifty_quant.analytics.metrics import periods_per_year

    return periods_per_year(pd.DatetimeIndex(index))
