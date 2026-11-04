"""Walk-forward evaluation.

The ported strategies are *not* re-fitted: their parameters come from the
published examples and are never optimised on this data. Walk-forward therefore
answers a narrower question than it would for a tuning workflow: does a strategy
that looked acceptable on an earlier stretch keep behaving acceptably on the
stretch that follows?

Because nothing is re-fitted, the whole-period run can be sliced into rolling
train/test windows instead of re-running each window from scratch. That is both
cheaper and closer to how the strategy would actually be operated (a continuous
account, not a per-window restart). The one artefact of slicing is that a
position open at a window boundary carries its P&L into the next window; window
metrics are rebased to the window's first equity point, so the reported return is
the return *earned inside* the window.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from nifty_quant.analytics.metrics import compute_metrics


@dataclass(frozen=True)
class Fold:
    """One rolling train/test pair of bar ranges."""

    fold: int
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    train_bars: int
    test_bars: int

    def as_row(self) -> dict:
        return {
            "fold": self.fold,
            "train_start": self.train_start,
            "train_end": self.train_end,
            "train_bars": self.train_bars,
            "test_start": self.test_start,
            "test_end": self.test_end,
            "test_bars": self.test_bars,
        }


def generate_folds(
    index: pd.DatetimeIndex,
    *,
    train_bars: int = 750,
    test_bars: int = 250,
    step_bars: int | None = None,
    min_test_bars: int = 40,
) -> list[Fold]:
    """Rolling, non-overlapping test windows.

    ``step_bars`` defaults to ``test_bars`` so consecutive tests tile the history
    exactly once; passing a smaller step creates overlapping tests (more folds,
    correlated evidence -- use with care).
    """
    index = pd.DatetimeIndex(index).sort_values()
    n = len(index)
    if train_bars < 1 or test_bars < 1:
        raise ValueError("train_bars and test_bars must be positive")
    step = int(step_bars or test_bars)
    if step < 1:
        raise ValueError("step_bars must be positive")

    folds: list[Fold] = []
    start = 0
    number = 1
    while start + train_bars + min_test_bars <= n:
        train = index[start : start + train_bars]
        test = index[start + train_bars : start + train_bars + test_bars]
        if len(test) < min_test_bars:
            break
        folds.append(
            Fold(
                fold=number,
                train_start=train[0],
                train_end=train[-1],
                test_start=test[0],
                test_end=test[-1],
                train_bars=len(train),
                test_bars=len(test),
            )
        )
        number += 1
        start += step
    return folds


def window_metrics(
    equity: pd.Series,
    trades: pd.DataFrame,
    window_start: pd.Timestamp,
    window_end: pd.Timestamp,
) -> dict[str, float]:
    """Metrics measured strictly inside one window.

    The equity segment is rebased to its own first point, so ``total_return`` is
    the return earned inside the window. Only trades whose *entry* falls inside
    the window are counted; a trade opened before the window is excluded because
    its risk was taken before the window began.
    """
    if equity is None or equity.empty:
        return {}
    segment = equity.loc[window_start:window_end]
    if len(segment) < 2:
        return {}
    rebased = segment / float(segment.iloc[0]) * 100_000.0
    window_trades = pd.DataFrame()
    fees = slippage = 0.0
    if trades is not None and not trades.empty:
        entered = trades[
            (pd.DatetimeIndex(trades["entry_time"]) >= window_start)
            & (pd.DatetimeIndex(trades["entry_time"]) <= window_end)
        ]
        window_trades = entered.reset_index(drop=True)
        if not window_trades.empty:
            fees = float(window_trades["fees"].sum())
            slippage = float(window_trades["slippage_cost"].sum())
    return compute_metrics(
        rebased,
        window_trades,
        initial_capital=100_000.0,
        fees_paid=fees,
        slippage_paid=slippage,
    )


_METRIC_KEYS = ("total_return", "cagr", "sharpe", "sortino", "max_drawdown", "n_trades", "win_rate", "profit_factor", "expectancy")


def walkforward_table(runs, folds: list[Fold], *, prefix_is: str = "is_", prefix_oos: str = "oos_") -> pd.DataFrame:
    """Per strategy x fold in-sample and out-of-sample metrics.

    ``runs`` is any mapping ``strategy_id -> StrategyRun`` (or an object with a
    ``runs`` attribute, such as ``ResearchResults``).
    """
    runs = getattr(runs, "runs", runs)
    rows: list[dict] = []
    for strategy_id, run in runs.items():
        for run_slice in run.slices:
            trades = run_slice.trades
            equity = run_slice.equity.get("equity") if not run_slice.equity.empty else None
            if equity is None or equity.empty:
                continue
            for fold in folds:
                is_metrics = window_metrics(equity, trades, fold.train_start, fold.train_end)
                oos_metrics = window_metrics(equity, trades, fold.test_start, fold.test_end)
                if not oos_metrics:
                    continue
                row = {
                    "strategy_id": strategy_id,
                    "sleeve": run_slice.label,
                    "fold": fold.fold,
                    "train_start": fold.train_start,
                    "test_start": fold.test_start,
                    "test_end": fold.test_end,
                }
                row.update({f"{prefix_is}{key}": is_metrics.get(key) for key in _METRIC_KEYS})
                row.update({f"{prefix_oos}{key}": oos_metrics.get(key) for key in _METRIC_KEYS})
                rows.append(row)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values(["strategy_id", "sleeve", "fold"]).reset_index(drop=True)


def fold_aggregate(table: pd.DataFrame) -> pd.DataFrame:
    """Collapse sleeves into one row per (strategy, fold).

    A single stock produces only a handful of trades per year, so a per-sleeve
    fold statistic is mostly noise. Aggregating across sleeves first turns the
    unit of evidence into "how did the strategy do across the universe in this
    fold", which is the question the walk-forward is meant to answer.
    """
    if table.empty:
        return pd.DataFrame()
    grouped = table.groupby(["strategy_id", "fold"], as_index=False).agg(
        sleeves=("sleeve", "nunique"),
        test_start=("test_start", "first"),
        test_end=("test_end", "first"),
        is_total_return=("is_total_return", "mean"),
        is_sharpe=("is_sharpe", "median"),
        is_n_trades=("is_n_trades", "sum"),
        oos_total_return=("oos_total_return", "mean"),
        oos_sharpe=("oos_sharpe", "median"),
        oos_n_trades=("oos_n_trades", "sum"),
        oos_max_drawdown=("oos_max_drawdown", "mean"),
        oos_win_rate=("oos_win_rate", "mean"),
    )
    return grouped.sort_values(["strategy_id", "fold"]).reset_index(drop=True)


def walkforward_summary(table: pd.DataFrame, *, min_trades: int = 10) -> pd.DataFrame:
    """Aggregate fold-level results into one row per strategy.

    ``table`` may hold one row per sleeve per fold; sleeves are averaged into a
    single fold value first (see ``fold_aggregate``). ``min_trades`` gates a fold on
    the *total* number of trades it contains across the universe.

    Reported instead of a single headline number:

    * ``oos_positive_fold_share`` - share of folds with positive out-of-sample return;
    * ``oos_median_sharpe`` - median out-of-sample Sharpe across folds;
    * ``is_oos_sharpe_decay`` - how much Sharpe survives the transition;
    * ``is_oos_rank_correlation`` - Spearman correlation between the in-sample and
      out-of-sample Sharpe of the same fold. A negative value is the classic sign
      of parameters that fit noise.
    """
    if table.empty:
        return pd.DataFrame()
    folds = fold_aggregate(table)
    usable = folds[folds["oos_n_trades"].fillna(0) >= min_trades]
    rows = []
    for strategy_id, group in folds.groupby("strategy_id", sort=True):
        sub = usable[usable["strategy_id"] == strategy_id]
        row = {
            "strategy_id": strategy_id,
            "folds": int(len(group)),
            "folds_with_trades": int(len(sub)),
            "sleeves": int(group["sleeves"].max()),
            "oos_positive_fold_share": float((sub["oos_total_return"] > 0).mean()) if len(sub) else np.nan,
            "oos_median_sharpe": float(sub["oos_sharpe"].median()) if len(sub) else np.nan,
            "is_median_sharpe": float(sub["is_sharpe"].median()) if len(sub) else np.nan,
            "oos_median_return": float(sub["oos_total_return"].median()) if len(sub) else np.nan,
        }
        if len(sub) > 2 and np.isfinite(sub["is_sharpe"]).sum() > 2:
            row["is_oos_sharpe_decay"] = row["is_median_sharpe"] - row["oos_median_sharpe"]
            row["is_oos_rank_correlation"] = float(sub["is_sharpe"].corr(sub["oos_sharpe"], method="spearman"))
        else:
            row["is_oos_sharpe_decay"] = np.nan
            row["is_oos_rank_correlation"] = np.nan
        rows.append(row)
    return pd.DataFrame(rows).sort_values("strategy_id").reset_index(drop=True)
