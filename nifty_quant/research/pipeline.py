"""End-to-end research orchestration and artifact persistence.

One call fetches/loads bars, audits them, runs every applicable strategy across
the universe, and writes a self-describing artifact directory:

    artifacts/<tag>/
        run_metadata.json      config, fingerprints, data provenance
        strategy_specs.csv     all twenty strategies incl. the excluded two
        strategy_metrics.csv   one row per strategy (aggregate)
        symbol_metrics.csv     one row per strategy x symbol
        ranking.csv            descriptive ranking + sample-size gate
        trades.csv             every round-trip trade
        signals.csv            every fill (entry/exit event log)
        monthly_returns.csv
        equity/<strategy>.parquet   per-sleeve equity + COMBINED column
        data_audit.csv         per-symbol data integrity findings

Nothing is written that is not derived from a recorded run, and the metadata
records the config and fingerprint needed to reproduce it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

from nifty_quant.config import PATHS, ResearchConfig
from nifty_quant.data.cache import BarStore
from nifty_quant.data.calendar import TradingCalendar
from nifty_quant.data.providers import build_provider
from nifty_quant.data.quality import audit_universe, assert_frames_usable
from nifty_quant.data.universe import Universe
from nifty_quant.engine.backtest import PORTFOLIO_LABEL, RunSlice, StrategyRun, run_backtest
from nifty_quant.research.results import ResearchResults
from nifty_quant.strategies.registry import applicable_ids, excluded_ids, selection, spec_table

LOG = logging.getLogger(__name__)

BENCHMARK_CACHE_SYMBOL = "IDX_NSEI"
COMBINED_COLUMN = "COMBINED"


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------
@dataclass
class MarketData:
    universe: Universe
    bars: dict[str, pd.DataFrame]
    benchmark: pd.DataFrame | None
    calendar: TradingCalendar
    audit: pd.DataFrame
    rejected: list[str]


def load_market_data(
    config: ResearchConfig | None = None,
    *,
    interval: str = "1d",
    symbols: Iterable[str] | None = None,
    force: bool = False,
    require_min_rows: int = 250,
) -> MarketData:
    """Load cache-first bars for the universe, plus the benchmark index."""
    config = config or ResearchConfig()
    universe = Universe.from_snapshot()
    wanted = list(symbols) if symbols is not None else universe.symbols

    store = BarStore(PATHS.cache)
    provider = build_provider("yfinance")
    LOG.info("loading %d symbols at %s (cache first)", len(wanted), interval)
    bars = store.get_many(wanted, interval, provider=provider, force=force)

    benchmark = None
    try:
        bench = store.get(
            config.benchmark_symbol,
            interval,
            provider=provider,
            force=force,
        )
        if bench is not None and not bench.empty:
            benchmark = bench
    except Exception as exc:  # noqa: BLE001 - the benchmark is optional context
        LOG.warning("benchmark %s unavailable: %s", config.benchmark_symbol, exc)

    calendar = TradingCalendar.from_frames(
        {**bars, **({BENCHMARK_CACHE_SYMBOL: benchmark} if benchmark is not None else {})},
        source=config.benchmark_symbol,
    )
    rejected = assert_frames_usable(bars, min_rows=require_min_rows, raise_on_error=False)
    usable = {symbol: frame for symbol, frame in bars.items() if symbol not in rejected}
    if not usable:
        raise RuntimeError("no symbol passed the data integrity check")

    audit = audit_universe(usable, calendar)
    return MarketData(
        universe=universe,
        bars=usable,
        benchmark=benchmark,
        calendar=calendar,
        audit=audit,
        rejected=rejected,
    )


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------
def run_research(
    config: ResearchConfig | None = None,
    *,
    interval: str = "1d",
    strategy_ids: Iterable[str] | None = None,
    symbols: Iterable[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    equity_only: bool = True,
    data: MarketData | None = None,
    force: bool = False,
    params_by_strategy: Mapping[str, Mapping[str, Any]] | None = None,
) -> ResearchResults:
    """Run every requested strategy across the universe and return the results."""
    config = config or ResearchConfig()
    data = data or load_market_data(config, interval=interval, symbols=symbols, force=force)

    ids = selection(strategy_ids, equity_only=equity_only)
    LOG.info("running %d strategies over %d symbols", len(ids), len(data.bars))

    runs: dict[str, StrategyRun] = {}
    for strategy_id in ids:
        params = (params_by_strategy or {}).get(strategy_id)
        runs[strategy_id] = run_backtest(
            strategy_id,
            data.bars,
            config=config,
            params=params,
            start=start,
            end=end,
            interval=interval,
        )

    metadata = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "interval": interval,
        "universe": data.universe.name,
        "universe_size": len(data.universe),
        "symbols_run": len(data.bars),
        "symbols_rejected": ",".join(sorted(data.rejected)) if data.rejected else "",
        "strategies": ",".join(ids),
        "strategies_excluded": ",".join(excluded_ids()),
        "start": str(start) if start else str(min(f.index[0] for f in data.bars.values()).date()),
        "end": str(end) if end else str(max(f.index[-1] for f in data.bars.values()).date()),
        "sessions": int(len(data.calendar)),
        "benchmark": config.benchmark_symbol,
        "survivorship_bias_exposed": data.universe.survivorship_bias_exposed,
        "config": config.as_dict(),
    }
    return ResearchResults(runs=runs, metadata=metadata)


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------
def _artifact_dir(tag: str | None = None, root: Path | str | None = None) -> Path:
    root = Path(root) if root is not None else PATHS.artifacts
    name = tag or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path(root) / name


def save_results(
    results: ResearchResults,
    *,
    tag: str | None = None,
    root: Path | str | None = None,
    data_audit: pd.DataFrame | None = None,
    equity_per_sleeve: bool = True,
) -> Path:
    """Write the full artifact set and return the directory."""
    target = _artifact_dir(tag, root)
    (target / "equity").mkdir(parents=True, exist_ok=True)

    metadata = dict(results.metadata)
    metadata["artifact_dir"] = str(target)
    metadata["saved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    metadata["strategy_fingerprints"] = {
        sid: run.fingerprint for sid, run in results.runs.items()
    }
    (target / "run_metadata.json").write_text(json.dumps(metadata, indent=2, default=str), encoding="utf-8")

    spec_table().to_csv(target / "strategy_specs.csv", index=False)
    results.strategy_metrics().to_csv(target / "strategy_metrics.csv", index=False)
    results.symbol_metrics().to_csv(target / "symbol_metrics.csv", index=False)
    results.ranking().to_csv(target / "ranking.csv", index=False)
    results.all_trades().to_csv(target / "trades.csv", index=False)
    results.signal_history().to_csv(target / "signals.csv", index=False)
    results.monthly_return_panel().to_csv(target / "monthly_returns.csv", index=False)
    if data_audit is not None and not data_audit.empty:
        data_audit.to_csv(target / "data_audit.csv", index=False)

    for strategy_id, run in results.runs.items():
        frame = pd.DataFrame()
        if equity_per_sleeve:
            frame = pd.DataFrame(
                {s.label: s.equity["equity"] for s in run.slices if not s.equity.empty}
            )
        combined = run.combined_equity()
        if not combined.empty:
            frame[COMBINED_COLUMN] = combined
        if not frame.empty:
            frame.to_parquet(target / "equity" / f"{strategy_id}.parquet")

    LOG.info("artifacts written to %s", target)
    return target


def load_results(dir_path: str | Path) -> ResearchResults:
    """Reload a saved artifact directory into the same result objects.

    Per-sleeve equity, metrics, closed trades and fills are restored from the
    artifacts, so a reloaded run reproduces both the trade-derived statistics
    (win rate, profit factor, expectancy) and the entry/exit signal log instead
    of silently reporting empty frames. Order-level state is not reloaded: it is
    not persisted per sleeve, and the status of an order that never filled is
    already visible in ``orders_active_at_end`` on a live run.
    """
    from nifty_quant.strategies.registry import spec as get_spec

    root = Path(dir_path)
    metadata = json.loads((root / "run_metadata.json").read_text(encoding="utf-8"))
    symbol_metrics = _read_csv(root / "symbol_metrics.csv")
    trades = _read_csv(root / "trades.csv")
    signals = _read_csv(root / "signals.csv")
    for frame in (symbol_metrics, trades):
        for column in ("entry_time", "exit_time"):
            if column in frame.columns:
                frame[column] = pd.to_datetime(frame[column])
    if "timestamp" in signals.columns:
        signals["timestamp"] = pd.to_datetime(signals["timestamp"])
    strategies = [s for s in str(metadata.get("strategies", "")).split(",") if s]

    runs: dict[str, StrategyRun] = {}
    for strategy_id in strategies:
        spec = get_spec(strategy_id)
        equity_path = root / "equity" / f"{strategy_id}.parquet"
        if not equity_path.exists():
            continue
        equity = pd.read_parquet(equity_path)
        if equity.empty:
            continue

        combined_present = COMBINED_COLUMN in equity.columns
        labels = [c for c in equity.columns if c != COMBINED_COLUMN]
        strategy_trades = (
            trades[trades["strategy_id"] == strategy_id].reset_index(drop=True)
            if not trades.empty and "strategy_id" in trades.columns
            else pd.DataFrame()
        )
        strategy_signals = (
            signals[signals["strategy_id"] == strategy_id].reset_index(drop=True)
            if not signals.empty and "strategy_id" in signals.columns
            else pd.DataFrame()
        )
        slices: list[RunSlice] = []
        for label in labels:
            metrics = {}
            if not symbol_metrics.empty:
                match = symbol_metrics[
                    (symbol_metrics["strategy_id"] == strategy_id)
                    & (symbol_metrics["symbol"] == label)
                ]
                if not match.empty:
                    metrics = {
                        k: (v.item() if hasattr(v, "item") else v)
                        for k, v in match.iloc[0].items()
                        if k not in ("strategy_id", "symbol") and pd.notna(v)
                    }
            slices.append(
                RunSlice(
                    label=label,
                    equity=pd.DataFrame({"equity": equity[label].dropna()}),
                    fills=_slice_fills(strategy_signals, label),
                    orders=pd.DataFrame(),
                    trades=_slice_trades(strategy_trades, label),
                    metrics=metrics,
                )
            )
        if combined_present and not labels:
            slices.append(
                RunSlice(
                    label=PORTFOLIO_LABEL,
                    equity=pd.DataFrame({"equity": equity[COMBINED_COLUMN]}),
                    fills=_slice_fills(strategy_signals, PORTFOLIO_LABEL),
                    orders=pd.DataFrame(),
                    trades=strategy_trades,
                    metrics={},
                )
            )
        if not slices:
            continue
        runs[strategy_id] = StrategyRun(
            strategy_id=strategy_id,
            spec=spec,
            params={},
            interval=str(metadata.get("interval", "1d")),
            mode=spec.run_mode,
            report_start=pd.Timestamp(metadata["start"]) if metadata.get("start") else None,
            report_end=pd.Timestamp(metadata["end"]) if metadata.get("end") else None,
            initial_capital=float(
                metadata.get("config", {}).get("engine", {}).get("initial_capital", 100_000.0)
            ),
            slices=slices,
            fingerprint=str(metadata.get("strategy_fingerprints", {}).get(strategy_id, "")),
            config=metadata.get("config", {}),
        )
    return ResearchResults(runs=runs, metadata=metadata)


def _slice_trades(strategy_trades: pd.DataFrame, label: str) -> pd.DataFrame:
    """Trades belonging to one sleeve (a portfolio sleeve holds every symbol)."""
    if strategy_trades.empty:
        return strategy_trades
    if label == PORTFOLIO_LABEL or "symbol" not in strategy_trades.columns:
        return strategy_trades
    return strategy_trades[strategy_trades["symbol"] == label].reset_index(drop=True)


def _slice_fills(strategy_fills: pd.DataFrame, label: str) -> pd.DataFrame:
    """Fills belonging to one sleeve, keyed on the instrument column."""
    if strategy_fills.empty:
        return strategy_fills
    frame = strategy_fills.drop(columns=["strategy_id"], errors="ignore")
    if label == PORTFOLIO_LABEL:
        return frame.reset_index(drop=True)
    key = next((c for c in ("symbol", "instrument") if c in frame.columns), None)
    if key is None:
        return pd.DataFrame()
    return frame[frame[key].astype(str) == label].reset_index(drop=True)


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def latest_artifact(root: Path | str | None = None) -> Path | None:
    root = Path(root) if root is not None else PATHS.artifacts
    if not root.exists():
        return None
    candidates = [p for p in root.iterdir() if p.is_dir() and (p / "run_metadata.json").exists()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


# --------------------------------------------------------------------------
# Small helpers used by the CLI and the dashboard
# --------------------------------------------------------------------------
def universe_symbols() -> list[str]:
    return Universe.from_snapshot().symbols


def available_strategies() -> dict[str, list[str]]:
    return {
        "applicable": applicable_ids(equity_only=True),
        "excluded": excluded_ids(),
    }


def coverage_report(bars: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    rows = []
    for symbol, frame in sorted(bars.items()):
        rows.append(
            {
                "symbol": symbol,
                "rows": int(len(frame)),
                "start": frame.index[0].date().isoformat() if len(frame) else "",
                "end": frame.index[-1].date().isoformat() if len(frame) else "",
                "years": round(
                    (frame.index[-1] - frame.index[0]).days / 365.25, 2
                )
                if len(frame)
                else 0.0,
                "zero_volume_rows": int((frame["volume"].fillna(0) <= 0).sum()),
                "nan_rows": int(frame[["open", "high", "low", "close"]].isna().any(axis=1).sum()),
            }
        )
    return pd.DataFrame(rows)


def summarise_universe(bars: Mapping[str, pd.DataFrame]) -> dict[str, Any]:
    rows = [len(f) for f in bars.values()]
    if not rows:
        return {"symbols": 0}
    return {
        "symbols": len(bars),
        "median_rows": float(np.median(rows)),
        "min_rows": int(min(rows)),
        "max_rows": int(max(rows)),
        "earliest": min(f.index[0] for f in bars.values()).date().isoformat(),
        "latest": max(f.index[-1] for f in bars.values()).date().isoformat(),
    }
