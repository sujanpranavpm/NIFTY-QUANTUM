"""In-memory aggregation of a completed research run.

`ResearchResults` is the single object the dashboard, the API and the CSV
exports all read from, so every surface shows the same numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

import numpy as np
import pandas as pd

from nifty_quant.engine.backtest import PORTFOLIO_LABEL, StrategyRun

MIN_TRADES_FOR_RANKING = 30


@dataclass
class ResearchResults:
    """All strategy runs of one research pass, plus run metadata."""

    runs: dict[str, StrategyRun] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    # ---- access ---------------------------------------------------------
    @property
    def strategy_ids(self) -> list[str]:
        return list(self.runs)

    def run(self, strategy_id: str) -> StrategyRun:
        try:
            return self.runs[strategy_id]
        except KeyError as exc:
            raise KeyError(f"strategy {strategy_id!r} is not part of this result set") from exc

    # ---- comparison tables ---------------------------------------------
    def strategy_metrics(self) -> pd.DataFrame:
        rows = []
        for strategy_id, run in self.runs.items():
            metrics = run.aggregate_metrics()
            per_symbol = run.metrics_by_symbol()
            tradable = per_symbol[per_symbol["n_trades"].fillna(0) > 0]
            row = {
                "strategy_id": strategy_id,
                "name": run.spec.name,
                "family": run.spec.family,
                "timeframe": run.spec.timeframe,
                "mode": run.mode,
                "applicable_to_nse_equity": run.spec.applicable_to_nse_equity,
                "n_symbols": int(len(run.slices)),
                "n_symbols_traded": int(len(tradable)),
                "fingerprint": run.fingerprint,
            }
            row.update(metrics)
            if not tradable.empty:
                row["median_symbol_sharpe"] = float(tradable["sharpe"].median())
                row["pct_symbols_positive"] = float((tradable["total_return"] > 0).mean())
                row["pct_symbols_beating_cash"] = float((tradable["net_profit"] > 0).mean())
            else:
                row["median_symbol_sharpe"] = np.nan
                row["pct_symbols_positive"] = np.nan
                row["pct_symbols_beating_cash"] = np.nan
            rows.append(row)
        frame = pd.DataFrame(rows)
        if frame.empty:
            return frame
        frame["sufficient_sample"] = frame["n_trades"] >= MIN_TRADES_FOR_RANKING
        return frame.sort_values("strategy_id").reset_index(drop=True)

    def symbol_metrics(self) -> pd.DataFrame:
        frames = [run.metrics_by_symbol() for run in self.runs.values()]
        frames = [f for f in frames if not f.empty]
        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames, ignore_index=True)
        return out.sort_values(["strategy_id", "symbol"]).reset_index(drop=True)

    def equity_curves(self) -> pd.DataFrame:
        series = {}
        for strategy_id, run in self.runs.items():
            combined = run.combined_equity()
            if not combined.empty:
                series[strategy_id] = combined
        if not series:
            return pd.DataFrame()
        return pd.DataFrame(series).sort_index()

    def drawdown_curves(self) -> pd.DataFrame:
        curves = self.equity_curves()
        if curves.empty:
            return curves
        return curves / curves.cummax() - 1.0

    def all_trades(self) -> pd.DataFrame:
        frames = [run.trades() for run in self.runs.values()]
        frames = [f for f in frames if not f.empty]
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True).sort_values("entry_time").reset_index(drop=True)

    def ranking(self) -> pd.DataFrame:
        """Descriptive ranking.

        Deliberately *not* ordered by total return: the sort key is the median
        per-symbol Sharpe, with a sample-size gate. Statistical significance is
        decided later by the validation layer (FDR-gated), never here.
        """
        metrics = self.strategy_metrics()
        if metrics.empty:
            return metrics
        ranked = metrics.copy()
        ranked["rank_score"] = ranked["median_symbol_sharpe"].fillna(-np.inf)
        ranked.loc[~ranked["sufficient_sample"], "rank_score"] = -np.inf
        ranked = ranked.sort_values(
            ["rank_score", "pct_symbols_positive", "n_trades"],
            ascending=[False, False, False],
        )
        ranked["rank"] = np.arange(1, len(ranked) + 1)
        return ranked.reset_index(drop=True)

    # ---- per-strategy detail -------------------------------------------
    def monthly_returns(self, strategy_id: str) -> pd.DataFrame:
        from nifty_quant.analytics.metrics import monthly_returns

        return monthly_returns(self.run(strategy_id).combined_equity())

    def yearly_returns(self, strategy_id: str) -> pd.Series:
        from nifty_quant.analytics.metrics import yearly_returns

        return yearly_returns(self.run(strategy_id).combined_equity())

    def monthly_return_panel(self) -> pd.DataFrame:
        rows = []
        for strategy_id in self.runs:
            table = self.monthly_returns(strategy_id)
            if table.empty:
                continue
            stacked = table.stack().rename("return").reset_index()
            stacked.columns = ["year", "month", "return"]
            stacked["strategy_id"] = strategy_id
            rows.append(stacked)
        if not rows:
            return pd.DataFrame()
        return pd.concat(rows, ignore_index=True)[["strategy_id", "year", "month", "return"]]

    def signal_history(self) -> pd.DataFrame:
        """Every recorded fill, across strategies, as a trade/event log."""
        frames = []
        for strategy_id, run in self.runs.items():
            fills = run.fills()
            if fills.empty:
                continue
            fills = fills.copy()
            fills["strategy_id"] = strategy_id
            frames.append(fills)
        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames, ignore_index=True)
        return out.sort_values("timestamp").reset_index(drop=True)

    # ---- summary --------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        metrics = self.strategy_metrics()
        traded = metrics[metrics.get("n_trades", 0).fillna(0) > 0] if not metrics.empty else metrics
        return {
            "strategies_run": int(len(self.runs)),
            "strategies_with_trades": int(len(traded)) if not traded.empty else 0,
            "symbols": int(
                max((len(r.slices) for r in self.runs.values()), default=0)
            ),
            "metadata": self.metadata,
        }

    def filter_symbols(self, symbols: list[str]) -> "ResearchResults":
        """View restricted to a symbol subset (labels that do not exist are ignored)."""
        keep = set(symbols)
        filtered: dict[str, StrategyRun] = {}
        for strategy_id, run in self.runs.items():
            slices = [s for s in run.slices if s.label in keep or s.label == PORTFOLIO_LABEL]
            if not slices:
                continue
            filtered[strategy_id] = StrategyRun(
                strategy_id=run.strategy_id,
                spec=run.spec,
                params=run.params,
                interval=run.interval,
                mode=run.mode,
                report_start=run.report_start,
                report_end=run.report_end,
                initial_capital=run.initial_capital,
                slices=slices,
                fingerprint=run.fingerprint,
                config=run.config,
            )
        return ResearchResults(runs=filtered, metadata=dict(self.metadata))


def flatten_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """One-level flatten, used for the run manifest export."""
    flat: dict[str, Any] = {}
    for key, value in metadata.items():
        if isinstance(value, Mapping):
            for inner_key, inner_value in value.items():
                flat[f"{key}.{inner_key}"] = inner_value
        else:
            flat[key] = value
    return flat
