"""Display formatting shared by the HTML report and the figure labels.

The CSVs deliberately keep raw floats; this module is the only place where a
number becomes a string for a human. Keeping the rules in one place means a
percentage is never shown as a ratio in one panel and as a fraction in another.
"""

from __future__ import annotations

import math
from typing import Iterable

import numpy as np
import pandas as pd

MISSING = "–"

PERCENT_COLUMNS = {
    "total_return",
    "cagr",
    "max_drawdown",
    "win_rate",
    "annualised_volatility",
    "pct_symbols_positive",
    "pct_symbols_beating_cash",
    "best_symbol_pnl_share",
    "oos_return_retention",
    "positive_neighbour_share",
    "spread",
    "oos_positive_fold_share",
    "compounded_return",
    "share_of_bars",
    "mean_bar_return",
    "volatility",
    "dev_total_return",
    "oos_total_return",
    "is_total_return",
    "oos_median_return",
    "base_total_return",
    "worst_total_return",
    "sliced_return",
    "restarted_return",
    "difference",
    "benchmark_return",
    "buy_hold_return",
}

PERCENT_SUFFIXES = ("_return", "_rate", "_share", "_retention", "_drawdown", "_volatility")

MONEY_COLUMNS = {
    "initial_capital",
    "final_equity",
    "net_profit",
    "fees_paid",
    "slippage_paid",
    "turnover",
    "net_realised_pnl",
    "fees_total_trades",
    "slippage_total_trades",
    "gross_profit",
    "gross_loss",
    "avg_win",
    "avg_loss",
    "expectancy",
    "open_pnl_end",
    "total_pnl",
    "avg_pnl",
    "gross_exposure",
    "market_value",
    "cash",
    "equity",
}

COUNT_COLUMNS = {
    "n_trades",
    "n_symbols",
    "n_symbols_traded",
    "n_sleeves",
    "n_trials",
    "folds",
    "folds_with_trades",
    "sleeves",
    "bars",
    "sessions",
    "observations",
    "variants",
    "usable_variants",
    "checks_passed",
    "checks_failed",
    "checks_untested",
    "critical_failures",
    "open_positions_end",
    "positions",
}

RATIO_COLUMNS = {
    "sharpe",
    "sortino",
    "calmar",
    "profit_factor",
    "psr",
    "deflated_sharpe",
    "expected_max_sharpe",
    "annualised_sharpe",
    "median_symbol_sharpe",
    "oos_median_sharpe",
    "is_median_sharpe",
    "is_oos_sharpe_decay",
    "is_oos_rank_correlation",
    "symbol_return_correlation",
    "rank_score",
    "turnover_per_year",
    "avg_holding_bars",
    "factor",
    "value",
}


def is_percent(name: str) -> bool:
    if name in PERCENT_COLUMNS:
        return True
    return name.endswith(PERCENT_SUFFIXES)


def is_money(name: str) -> bool:
    return name in MONEY_COLUMNS


def is_count(name: str) -> bool:
    return name in COUNT_COLUMNS or name.startswith("n_")


def is_ratio(name: str) -> bool:
    return name in RATIO_COLUMNS


def is_pvalue(name: str) -> bool:
    return name.endswith("pvalue") or name.endswith("qvalue") or name == "p_value"


def format_value(value, name: str = "") -> str:
    """One cell, formatted by the column's declared kind."""
    if value is None:
        return MISSING
    if isinstance(value, (pd.Timestamp,)):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, str):
        return value
    if isinstance(value, (np.bool_, bool)):
        return "yes" if bool(value) else "no"
    if value is pd.NaT:
        return MISSING
    if isinstance(value, (int, np.integer)) and not is_percent(name):
        return f"{int(value):,}"
    if isinstance(value, (float, np.floating)):
        numeric = float(value)
        if not math.isfinite(numeric):
            return MISSING
        if is_pvalue(name) and 0.0 <= numeric <= 1.0:
            return f"{numeric:.3g}"
        if is_percent(name):
            return f"{numeric:.1%}"
        if is_money(name):
            return f"{numeric:,.0f}"
        if is_count(name):
            return f"{numeric:,.0f}"
        if is_ratio(name):
            return f"{numeric:.2f}"
        return f"{numeric:,.4g}"
    return str(value)


def header_label(name: str) -> str:
    label = str(name).replace("_", " ")
    if is_money(name):
        return f"{label} (₹)"
    if is_percent(name):
        return f"{label} (%)"
    return label


def format_frame(
    frame: pd.DataFrame,
    columns: Iterable[str] | None = None,
    *,
    drop_all_missing: bool = True,
) -> pd.DataFrame:
    """A copy of ``frame`` with every cell rendered as display text."""
    if frame is None or frame.empty:
        return pd.DataFrame()
    selected = [c for c in (columns or frame.columns) if c in frame.columns]
    out = frame.loc[:, selected].copy()
    if drop_all_missing:
        keep = [c for c in out.columns if out[c].notna().any() or c in ("strategy_id", "symbol", "check", "param", "regime")]
        out = out.loc[:, keep]
    rendered = pd.DataFrame(index=out.index)
    for column in out.columns:
        rendered[header_label(column)] = [format_value(v, str(column)) for v in out[column]]
    return rendered
