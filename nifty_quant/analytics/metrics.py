"""Performance metrics, computed only from recorded equity and fills.

Every number the deliverables quote comes from one of these functions. There is
no hardcoded or remembered performance figure anywhere in the project.

Annualisation is derived from the observed bar spacing of the run itself rather
than assumed, because the same code serves daily and intraday strategies.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

TRADING_DAYS_PER_YEAR = 252


def periods_per_year(index: pd.DatetimeIndex) -> float:
    """Bars per year implied by the run's own timestamp spacing."""
    if len(index) < 2:
        return float(TRADING_DAYS_PER_YEAR)
    span = (index[-1] - index[0]).total_seconds() / (365.25 * 24 * 3600)
    if span <= 0:
        return float(TRADING_DAYS_PER_YEAR)
    return max(1.0, (len(index) - 1) / span)


def total_return(equity: pd.Series) -> float:
    if equity.empty or equity.iloc[0] <= 0:
        return float("nan")
    return float(equity.iloc[-1] / equity.iloc[0] - 1.0)


def cagr(equity: pd.Series) -> float:
    if len(equity) < 2 or equity.iloc[0] <= 0 or equity.iloc[-1] <= 0:
        return float("nan")
    span_days = (equity.index[-1] - equity.index[0]).total_seconds() / 86400.0
    if span_days <= 0:
        return float("nan")
    years = span_days / 365.25
    if years <= 0:
        return float("nan")
    return float((equity.iloc[-1] / equity.iloc[0]) ** (1.0 / years) - 1.0)


def drawdown_series(equity: pd.Series) -> pd.Series:
    if equity.empty:
        return equity
    peak = equity.cummax()
    return equity / peak - 1.0


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return float("nan")
    return float(drawdown_series(equity).min())


def max_drawdown_date(equity: pd.Series) -> pd.Timestamp | None:
    if equity.empty:
        return None
    dd = drawdown_series(equity)
    return dd.idxmin()


def _returns(equity: pd.Series) -> pd.Series:
    if len(equity) < 2:
        return pd.Series(dtype=float)
    return equity.pct_change().replace([np.inf, -np.inf], np.nan).dropna()


def sharpe_ratio(equity: pd.Series, risk_free: float = 0.0) -> float:
    rets = _returns(equity)
    if len(rets) < 3:
        return float("nan")
    ppy = periods_per_year(pd.DatetimeIndex(equity.index))
    excess = rets - risk_free / ppy
    sd = float(excess.std(ddof=1))
    if sd <= 0 or not math.isfinite(sd):
        return float("nan")
    return float(excess.mean() / sd * math.sqrt(ppy))


def sortino_ratio(equity: pd.Series, risk_free: float = 0.0) -> float:
    rets = _returns(equity)
    if len(rets) < 3:
        return float("nan")
    ppy = periods_per_year(pd.DatetimeIndex(equity.index))
    excess = rets - risk_free / ppy
    downside = excess[excess < 0]
    if len(downside) < 2:
        return float("nan")
    dd = float(downside.std(ddof=1))
    if dd <= 0 or not math.isfinite(dd):
        return float("nan")
    return float(excess.mean() / dd * math.sqrt(ppy))


def annualised_volatility(equity: pd.Series) -> float:
    rets = _returns(equity)
    if len(rets) < 3:
        return float("nan")
    ppy = periods_per_year(pd.DatetimeIndex(equity.index))
    return float(rets.std(ddof=1) * math.sqrt(ppy))


def compute_trade_stats(trades: pd.DataFrame) -> dict[str, float]:
    """Win rate, payoff, expectancy and cost totals over *closed* trades."""
    empty = {
        "n_trades": 0.0,
        "win_rate": float("nan"),
        "profit_factor": float("nan"),
        "avg_win": float("nan"),
        "avg_loss": float("nan"),
        "expectancy": float("nan"),
        "avg_holding_bars": float("nan"),
        "gross_profit": 0.0,
        "gross_loss": 0.0,
        "fees_total_trades": 0.0,
        "slippage_total_trades": 0.0,
        "net_realised_pnl": 0.0,
        "open_positions_end": 0,
        "open_pnl_end": 0.0,
    }
    if trades is None or trades.empty:
        return empty

    closed = trades[~trades["is_open"]]
    opened = trades[trades["is_open"]]

    if closed.empty:
        empty["open_positions_end"] = int(len(opened))
        empty["open_pnl_end"] = float(opened["net_pnl"].sum()) if not opened.empty else 0.0
        return empty

    pnl = closed["net_pnl"].astype(float)
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    gross_profit = float(wins.sum())
    gross_loss = float(losses.sum())
    return {
        "n_trades": float(len(closed)),
        "win_rate": float(len(wins) / len(closed)),
        "profit_factor": float(gross_profit / abs(gross_loss)) if gross_loss < 0 else float("nan"),
        "avg_win": float(wins.mean()) if len(wins) else float("nan"),
        "avg_loss": float(losses.mean()) if len(losses) else float("nan"),
        "expectancy": float(pnl.mean()),
        "avg_holding_bars": float(closed["bars_held"].mean()),
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "fees_total_trades": float(closed["fees"].sum()),
        "slippage_total_trades": float(closed["slippage_cost"].sum()),
        "net_realised_pnl": float(pnl.sum()),
        "open_positions_end": int(len(opened)),
        "open_pnl_end": float(opened["net_pnl"].sum()) if not opened.empty else 0.0,
    }


def compute_metrics(
    equity: pd.Series,
    trades: pd.DataFrame,
    *,
    initial_capital: float,
    fees_paid: float = 0.0,
    slippage_paid: float = 0.0,
    turnover: float = 0.0,
    sessions: int | None = None,
) -> dict[str, float]:
    """Full metric set for one strategy x symbol run."""
    equity = equity.astype(float)
    stats = compute_trade_stats(trades)
    ppy = periods_per_year(pd.DatetimeIndex(equity.index)) if len(equity) else float("nan")

    metrics: dict[str, float] = {
        "initial_capital": float(initial_capital),
        "final_equity": float(equity.iloc[-1]) if len(equity) else float("nan"),
        "total_return": total_return(equity),
        "cagr": cagr(equity),
        "max_drawdown": max_drawdown(equity),
        "annualised_volatility": annualised_volatility(equity),
        "sharpe": sharpe_ratio(equity),
        "sortino": sortino_ratio(equity),
        "calmar": (
            cagr(equity) / abs(max_drawdown(equity))
            if len(equity) and max_drawdown(equity) < 0
            else float("nan")
        ),
        "bars": float(len(equity)),
        "periods_per_year": ppy,
        "fees_paid": float(fees_paid),
        "slippage_paid": float(slippage_paid),
        "turnover": float(turnover),
        "turnover_per_year": (
            float(turnover) / initial_capital / ((len(equity) - 1) / ppy)
            if len(equity) > 1 and ppy > 0 and initial_capital > 0
            else float("nan")
        ),
        "net_profit": (
            float(equity.iloc[-1] - initial_capital) if len(equity) else float("nan")
        ),
    }
    metrics.update(stats)
    if sessions is not None:
        metrics["sessions"] = float(sessions)
    return metrics


def monthly_returns(equity: pd.Series) -> pd.DataFrame:
    """Month x year table of percentage returns."""
    if equity.empty:
        return pd.DataFrame()
    monthly = equity.resample("ME").last().pct_change().dropna()
    if monthly.empty:
        return pd.DataFrame()
    frame = pd.DataFrame(
        {
            "year": monthly.index.year,
            "month": monthly.index.month,
            "return": monthly.values,
        }
    )
    table = frame.pivot(index="year", columns="month", values="return")
    table = table.reindex(columns=range(1, 13))
    table.columns = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ]
    return table


def yearly_returns(equity: pd.Series) -> pd.Series:
    if equity.empty:
        return pd.Series(dtype=float)
    yearly = equity.resample("YE").last().pct_change()
    if len(yearly) and equity.index[0].year == yearly.index[0].year:
        # First period return is measured from the initial equity point.
        yearly.iloc[0] = equity.resample("YE").last().iloc[0] / equity.iloc[0] - 1.0
    yearly.index = yearly.index.year
    return yearly.dropna()


def summarise(metrics: dict[str, float]) -> str:
    """One-line human summary used in logs and the CLI."""
    def fmt(key: str, pattern: str = "{:.2f}") -> str:
        value = metrics.get(key)
        if value is None or (isinstance(value, float) and not math.isfinite(value)):
            return "n/a"
        return pattern.format(value)

    return (
        f"return={fmt('total_return', '{:.1%}')} cagr={fmt('cagr', '{:.1%}')} "
        f"dd={fmt('max_drawdown', '{:.1%}')} sharpe={fmt('sharpe')} "
        f"trades={int(metrics.get('n_trades', 0))} win={fmt('win_rate', '{:.1%}')} "
        f"pf={fmt('profit_factor')}"
    )
