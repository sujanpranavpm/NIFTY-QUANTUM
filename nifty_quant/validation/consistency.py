"""Consistency across stocks, and persistence across the development split.

Two different questions, both of which catch a strategy that only looks good in
aggregate:

* **Stock-by-stock consistency** - does the edge appear in most names, or is the
  aggregate driven by one lucky stock? Reported as the share of symbols that are
  profitable, a sign test on that share, and the concentration of total P&L in the
  single best name.
* **Persistence** - does the ranking of *stocks* hold between the development
  window and the held-out window? A strategy whose per-symbol results are
  uncorrelated across the split was fitting noise in the earlier period.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from nifty_quant.validation.splits import Split
from nifty_quant.validation.stats import binomial_sign_test
from nifty_quant.validation.walkforward import window_metrics


def symbol_consistency(symbol_metrics: pd.DataFrame, *, min_trades: int = 5) -> pd.DataFrame:
    """Per-strategy dispersion of results across stocks."""
    if symbol_metrics is None or symbol_metrics.empty:
        return pd.DataFrame()
    frame = symbol_metrics.copy()
    if "n_trades" in frame.columns:
        frame = frame[frame["n_trades"].fillna(0) >= min_trades]
    if frame.empty:
        return pd.DataFrame()

    rows = []
    for strategy_id, group in frame.groupby("strategy_id", sort=True):
        returns = group["total_return"].astype(float).dropna()
        pnl = group["net_realised_pnl"].astype(float).dropna() if "net_realised_pnl" in group else pd.Series(dtype=float)
        positive = int((returns > 0).sum())
        negative = int((returns < 0).sum())
        total_pnl = float(pnl.sum()) if len(pnl) else float("nan")
        best_pnl = float(pnl.max()) if len(pnl) else float("nan")
        rows.append(
            {
                "strategy_id": strategy_id,
                "n_symbols": int(len(returns)),
                "pct_symbols_positive": float(positive / len(returns)) if len(returns) else np.nan,
                "sign_test_pvalue": binomial_sign_test(positive, negative),
                "median_symbol_return": float(returns.median()) if len(returns) else np.nan,
                "iqr_symbol_return": float(returns.quantile(0.75) - returns.quantile(0.25)) if len(returns) else np.nan,
                "worst_symbol_return": float(returns.min()) if len(returns) else np.nan,
                "best_symbol_return": float(returns.max()) if len(returns) else np.nan,
                "total_net_pnl": total_pnl,
                "best_symbol_pnl_share": (
                    float(best_pnl / total_pnl) if total_pnl and total_pnl > 0 and np.isfinite(best_pnl) else np.nan
                ),
            }
        )
    return pd.DataFrame(rows).sort_values("strategy_id").reset_index(drop=True)


def dev_oos_table(runs, split: Split, *, min_trades: int = 3) -> pd.DataFrame:
    """Development vs held-out metrics per strategy, from one continuous run.

    The run itself is continuous (see ``walkforward`` for why), so both windows
    measure the same account. ``oos_return_retention`` compares the held-out return
    with the development return over the same strategy; a value near or below zero
    means the strategy did not survive its own holdout.
    """
    runs = getattr(runs, "runs", runs)
    rows = []
    for strategy_id, run in runs.items():
        dev_rows, oos_rows = [], []
        for run_slice in run.slices:
            if run_slice.equity is None or run_slice.equity.empty:
                continue
            equity = run_slice.equity["equity"]
            trades = run_slice.trades
            dev = window_metrics(equity, trades, split.dev_start, split.dev_end)
            oos = window_metrics(equity, trades, split.oos_start, split.oos_end)
            if dev:
                dev_rows.append({**dev, "sleeve": run_slice.label})
            if oos:
                oos_rows.append({**oos, "sleeve": run_slice.label})
        if not oos_rows:
            continue
        dev_frame = pd.DataFrame(dev_rows)
        oos_frame = pd.DataFrame(oos_rows)
        row = {"strategy_id": strategy_id}
        for key in ("total_return", "sharpe", "max_drawdown", "n_trades", "win_rate", "profit_factor", "expectancy"):
            row[f"dev_{key}"] = float(dev_frame[key].mean()) if key in dev_frame else np.nan
            row[f"oos_{key}"] = float(oos_frame[key].mean()) if key in oos_frame else np.nan
        if dev_frame.empty:
            row["dev_by_symbol"] = {}
            row["oos_by_symbol"] = {}
            rows.append(row)
            continue
        # Cross-sectional persistence: do the same stocks work in both windows?
        merged = dev_frame.merge(oos_frame, on="sleeve", suffixes=("_dev", "_oos"))
        merged = merged[merged["n_trades_dev"].fillna(0) >= min_trades]
        if len(merged) >= 4:
            row["symbol_return_correlation"] = float(
                merged["total_return_dev"].corr(merged["total_return_oos"], method="spearman")
            )
        else:
            row["symbol_return_correlation"] = np.nan
        row["dev_by_symbol"] = dict(zip(dev_frame["sleeve"], dev_frame["total_return"]))
        row["oos_by_symbol"] = dict(zip(oos_frame["sleeve"], oos_frame["total_return"]))
        dev_return = row.get("dev_total_return")
        oos_return = row.get("oos_total_return")
        row["oos_return_retention"] = (
            float(oos_return / dev_return) if dev_return not in (None, 0) and np.isfinite(dev_return) and dev_return != 0 else np.nan
        )
        rows.append(row)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("strategy_id").reset_index(drop=True)
