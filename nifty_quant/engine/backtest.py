"""Single-strategy backtest runner.

Execution order for every bar is exactly the upstream contract:

1. match orders that were pending *before* this bar (``process_bar``),
2. book the fills into the portfolio,
3. mark the portfolio to this bar's close,
4. ask the strategy for orders (``on_bar``), which therefore cannot fill before
   the next bar.

That ordering is the anti-look-ahead guarantee of the whole system, and it is
asserted in the test suite rather than assumed.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping

import numpy as np
import pandas as pd

from nifty_quant.analytics.metrics import compute_metrics, drawdown_series
from nifty_quant.analytics.trades import extract_trades
from nifty_quant.config import ResearchConfig
from nifty_quant.engine.accounting import Portfolio
from nifty_quant.engine.costs import build_commission, build_slippage
from nifty_quant.engine.features import build_panels, panel_index
from nifty_quant.engine.fill_engine import FillEngine
from nifty_quant.engine.orders import BarData, OrderSide
from nifty_quant.engine.panel import row_map, to_views
from nifty_quant.strategies.base import InstrumentDataView, StrategySpec
from nifty_quant.strategies.registry import get_strategy, spec as get_spec

logger = logging.getLogger(__name__)

PORTFOLIO_LABEL = "PORTFOLIO"


@dataclass
class RunSlice:
    """One portfolio (a single stock sleeve, or the whole universe) after a run."""

    label: str
    equity: pd.DataFrame
    fills: pd.DataFrame
    orders: pd.DataFrame
    trades: pd.DataFrame
    metrics: dict[str, float]
    diagnostics: dict[str, Any] = field(default_factory=dict)
    # Positions and pending orders at the last processed bar, captured before
    # the end-of-run cancellation so a signal engine can read the strategy's
    # current intent without re-implementing the order logic.
    positions: dict[str, float] = field(default_factory=dict)
    pending: pd.DataFrame = field(default_factory=pd.DataFrame)


@dataclass
class StrategyRun:
    """Result of one strategy across one or more instruments."""

    strategy_id: str
    spec: StrategySpec
    params: dict[str, Any]
    interval: str
    mode: str
    report_start: pd.Timestamp | None
    report_end: pd.Timestamp | None
    initial_capital: float
    slices: list[RunSlice]
    fingerprint: str
    config: dict[str, Any] = field(default_factory=dict)

    # ---- convenience views ---------------------------------------------
    @property
    def symbols(self) -> list[str]:
        return [s.label for s in self.slices if s.label != PORTFOLIO_LABEL]

    def trades(self) -> pd.DataFrame:
        frames = [s.trades for s in self.slices if not s.trades.empty]
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    def fills(self) -> pd.DataFrame:
        frames = [s.fills for s in self.slices if not s.fills.empty]
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    def equity_by_symbol(self) -> pd.DataFrame:
        series = {
            s.label: s.equity["equity"] for s in self.slices if "equity" in s.equity.columns
        }
        if not series:
            return pd.DataFrame()
        return pd.DataFrame(series).sort_index()

    def combined_equity(self) -> pd.Series:
        """Equal-capital combination of the sleeves.

        Each sleeve starts with ``initial_capital``; sleeves that have not yet
        produced a bar are treated as idle cash at par, so an early start date in
        one name cannot inflate its weight in the aggregate.
        """
        if not self.slices:
            return pd.Series(dtype=float)
        if len(self.slices) == 1:
            return self.slices[0].equity["equity"]

        curves = pd.DataFrame(
            {s.label: s.equity["equity"] for s in self.slices if not s.equity.empty}
        ).sort_index()
        normalised = (curves / self.initial_capital).ffill().fillna(1.0)
        return normalised.mean(axis=1) * self.initial_capital

    def metrics_by_symbol(self) -> pd.DataFrame:
        rows = []
        for run_slice in self.slices:
            row = {"strategy_id": self.strategy_id, "symbol": run_slice.label}
            row.update(run_slice.metrics)
            row.update({f"diag_{k}": v for k, v in run_slice.diagnostics.items()})
            rows.append(row)
        return pd.DataFrame(rows)

    def aggregate_metrics(self) -> dict[str, float]:
        equity = self.combined_equity()
        if equity.empty:
            return {}
        trades = self.trades()
        fees = sum(s.metrics.get("fees_paid", 0.0) for s in self.slices)
        slippage = sum(s.metrics.get("slippage_paid", 0.0) for s in self.slices)
        turnover = sum(s.metrics.get("turnover", 0.0) for s in self.slices)
        metrics = compute_metrics(
            equity,
            trades,
            initial_capital=self.initial_capital,
            fees_paid=fees,
            slippage_paid=slippage,
            turnover=turnover,
        )
        metrics["strategy_id"] = self.strategy_id  # type: ignore[assignment]
        metrics["n_symbols"] = float(len(self.slices))
        return metrics  # type: ignore[return-value]


def _fingerprint(payload: Mapping[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


class _SymbolBars:
    """One instrument's OHLCV as plain numpy columns plus a timestamp map.

    Resolving ``frame.loc[date]`` costs about 0.4 ms per call, which dominates a
    50-instrument grid; a dict lookup into per-column arrays costs nothing.
    """

    __slots__ = ("open", "high", "low", "close", "volume", "_rows")

    def __init__(self, frame: pd.DataFrame, rows: dict | None = None) -> None:
        for name in ("open", "high", "low", "close", "volume"):
            if name in frame.columns:
                values = np.asarray(frame[name], dtype=float)
            else:
                values = np.zeros(len(frame))
            setattr(self, name, values)
        # ``rows`` is only valid when the caller verified the index matches.
        self._rows = rows if rows is not None else row_map(frame.index)

    def bar_at(self, timestamp) -> BarData | None:
        """The bar for ``timestamp``, or None on a holiday / bad close."""
        i = self._rows.get(timestamp)
        if i is None:
            return None
        close = float(self.close[i])
        if not np.isfinite(close) or close <= 0:
            return None
        return BarData(
            timestamp=timestamp,
            open=float(self.open[i]),
            high=float(self.high[i]),
            low=float(self.low[i]),
            close=close,
            volume=float(self.volume[i]),
        )


def _orders_frame(engine: FillEngine) -> pd.DataFrame:
    return _order_records(engine.order_history, status=True)


def _pending_orders_frame(engine: FillEngine) -> pd.DataFrame:
    return _order_records(engine.pending_orders, status=False)


def _order_records(orders: list, *, status: bool) -> pd.DataFrame:
    rows = []
    for order in orders:
        row = {
            "order_id": order.order_id,
            "symbol": order.instrument,
            "side": order.side.value,
            "quantity": order.quantity,
            "order_type": order.order_type.value,
            "limit_price": order.limit_price,
            "stop_price": order.stop_price,
            "limit_offset": order.limit_offset,
            "trail_amount": order.trail_amount,
            "trail_percent": order.trail_percent,
            "oco_pair_id": order.oco_pair_id,
            "expires_after_bars": order.expires_after_bars,
            "created_at": order.created_at,
            "tag": order.tag,
        }
        if status:
            row.update(
                {
                    "status": order.status.value,
                    "filled_qty": order.filled_qty,
                    "avg_fill_price": order.avg_fill_price,
                }
            )
        rows.append(row)
    return pd.DataFrame(rows)


def _nav_scale(spec: StrategySpec, params: Mapping[str, Any], mode: str, cfg: ResearchConfig) -> float:
    if mode == "portfolio" or cfg.engine.capital_utilisation == "as_published":
        return 1.0
    if cfg.engine.capital_utilisation != "full_sleeve":
        raise ValueError(
            f"unknown capital_utilisation {cfg.engine.capital_utilisation!r}; "
            "expected 'as_published' or 'full_sleeve'"
        )
    weight = float(params.get("weight", spec.default_weight) or spec.default_weight)
    if not np.isfinite(weight) or weight <= 0:
        return 1.0
    return 1.0 / weight


def _run_slice(
    *,
    strategy_id: str,
    spec: StrategySpec,
    symbols: list[str],
    label: str,
    bars: Mapping[str, pd.DataFrame],
    panels: Mapping[str, pd.DataFrame],
    index: pd.DatetimeIndex,
    config: ResearchConfig,
    params: Mapping[str, Any],
    mode: str,
    report_start: pd.Timestamp | None,
    interval: str,
) -> RunSlice:
    portfolio = Portfolio(config.engine.initial_capital)
    clipped = {"attempts": 0, "quantity": 0.0}

    def capacity(instrument: str, price: float, remaining: float, side: OrderSide) -> float:
        allowed = portfolio.affordable_quantity(price, remaining)
        if allowed < remaining - 1e-9:
            clipped["attempts"] += 1
            clipped["quantity"] += remaining - allowed
        return allowed

    engine = FillEngine(
        slippage=build_slippage(config.slippage),
        commission=build_commission(config.costs, include_costs=True),
        fill_at=config.engine.fill_at,
        max_volume_participation=config.engine.max_volume_participation,
        capacity_fn=capacity if config.engine.cash_guard else None,
    )
    rows = row_map(index)
    view = InstrumentDataView(to_views(panels, index, rows=rows))
    strategy = get_strategy(strategy_id, dict(params))
    universe = list(symbols)
    strategy.bind(engine, view, universe, portfolio.positions, portfolio.avg_entry)

    bar_sources = {
        symbol: _SymbolBars(
            bars[symbol], rows if bars[symbol].index.equals(index) else None
        )
        for symbol in universe
        if symbol in bars and len(bars[symbol])
    }
    nav_scale = _nav_scale(spec, params, mode, config)
    last_close: dict[str, float] = {}
    records: list[dict[str, Any]] = []

    for date in index:
        bars_today: dict[str, BarData] = {}
        for symbol, source in bar_sources.items():
            bar = source.bar_at(date)
            if bar is not None:
                bars_today[symbol] = bar

        for symbol, bar in bars_today.items():
            for fill in engine.process_bar(symbol, bar):
                portfolio.apply_fill(fill, date)
            last_close[symbol] = bar.close

        equity = portfolio.equity(last_close)
        records.append(
            {
                "timestamp": date,
                "equity": equity,
                "cash": portfolio.cash,
                "market_value": equity - portfolio.cash,
                "gross_exposure": portfolio.gross_exposure(last_close),
                "positions": sum(1 for q in portfolio.positions.values() if q != 0),
            }
        )

        if not bars_today:
            continue
        strategy.instruments = [s for s in universe if s in bars_today]
        strategy.on_bar(date, bars_today, portfolio.positions, equity * nav_scale)
        # Stamp the submission bar so the exports can prove that every fill is
        # strictly later than the order that produced it.
        for order in engine.pending_orders:
            if order.created_at is None:
                order.created_at = date

    # Nothing can fill after the last bar; record and cancel what is left.
    orders_active_at_end = len(engine.pending_orders)
    pending_frame = _pending_orders_frame(engine)
    final_positions = dict(portfolio.positions)
    engine.cancel_all()

    equity_frame = pd.DataFrame(records).set_index("timestamp") if records else pd.DataFrame()
    if not equity_frame.empty:
        equity_frame["drawdown"] = drawdown_series(equity_frame["equity"])

    fills_frame = pd.DataFrame(portfolio.fills)
    orders_frame = _orders_frame(engine)

    trades = extract_trades(
        fills_frame,
        strategy_id=strategy_id,
        index=index,
        last_prices=last_close,
    )

    # ---- trim to the reporting window -----------------------------------
    if report_start is not None and not equity_frame.empty:
        before = equity_frame.loc[: report_start]
        base = float(before["equity"].iloc[-1]) if len(before) else config.engine.initial_capital
        equity_frame = equity_frame.loc[report_start:]
        if not equity_frame.empty and base > 0:
            equity_frame["equity"] = equity_frame["equity"] / base * config.engine.initial_capital
            equity_frame["cash"] = equity_frame["cash"] / base * config.engine.initial_capital
            equity_frame["market_value"] = (
                equity_frame["market_value"] / base * config.engine.initial_capital
            )
            equity_frame["drawdown"] = drawdown_series(equity_frame["equity"])
        trades = trades.copy()
        if not trades.empty:
            trades["in_warmup"] = trades["entry_time"] < report_start
            trades = trades[trades["exit_time"] >= report_start].reset_index(drop=True)
        if not fills_frame.empty:
            fills_frame = fills_frame[fills_frame["timestamp"] >= report_start].reset_index(drop=True)

    metrics = compute_metrics(
        equity_frame["equity"] if not equity_frame.empty else pd.Series(dtype=float),
        trades,
        initial_capital=config.engine.initial_capital,
        fees_paid=portfolio.fees_paid,
        slippage_paid=portfolio.slippage_paid,
        turnover=portfolio.turnover,
    )

    diagnostics: dict[str, Any] = {
        "bars": int(len(index)),
        "sessions": int(pd.DatetimeIndex(index).normalize().nunique()) if len(index) else 0,
        "orders_submitted": int(len(orders_frame)),
        "orders_expired": int((orders_frame["status"] == "expired").sum()) if not orders_frame.empty else 0,
        "orders_cancelled": int((orders_frame["status"] == "cancelled").sum()) if not orders_frame.empty else 0,
        "orders_active_at_end": int(orders_active_at_end),
        "fills": int(len(fills_frame)),
        "cash_limited_attempts": int(clipped["attempts"]),
        "cash_clipped_quantity": float(clipped["quantity"]),
        "nav_scale": float(nav_scale),
    }
    if not trades.empty:
        diagnostics["open_positions_at_end"] = int(trades["is_open"].sum())
    else:
        diagnostics["open_positions_at_end"] = 0

    if interval != "1d" and not equity_frame.empty:
        per_session = equity_frame.groupby(equity_frame.index.normalize())["positions"].last()
        diagnostics["overnight_positions_at_session_end"] = int((per_session > 0).sum())

    return RunSlice(
        label=label,
        equity=equity_frame,
        fills=fills_frame,
        orders=orders_frame,
        trades=trades,
        metrics=metrics,
        diagnostics=diagnostics,
        positions=final_positions,
        pending=pending_frame,
    )


def run_backtest(
    strategy_id: str,
    bars: Mapping[str, pd.DataFrame],
    *,
    config: ResearchConfig | None = None,
    params: Mapping[str, Any] | None = None,
    start: str | pd.Timestamp | None = None,
    end: str | pd.Timestamp | None = None,
    interval: str = "1d",
    mode: str | None = None,
    rsi_method: str = "wilder",
) -> StrategyRun:
    """Run one strategy over one or many instruments.

    Args:
        strategy_id: ``orders_01`` .. ``orders_20``.
        bars: symbol -> OHLCV frame with canonical lowercase columns. Frames
            should include history *before* ``start`` when ``start`` is set, so
            indicators are warm at the first reported bar.
        mode: ``per_stock`` (default from the spec) gives every symbol its own
            capital sleeve; ``portfolio`` runs one account across all symbols.
    """
    config = config or ResearchConfig()
    spec = get_spec(strategy_id)
    merged_params = {**dict(params or {})}
    mode = mode or spec.run_mode
    if mode not in ("per_stock", "portfolio"):
        raise ValueError(f"unknown run mode {mode!r}")

    prepared: dict[str, pd.DataFrame] = {}
    for symbol, frame in bars.items():
        if frame is None or frame.empty:
            continue
        frame = frame.sort_index()
        if end is not None:
            frame = frame.loc[: pd.Timestamp(end)]
        if frame.empty:
            continue
        prepared[symbol] = frame
    if not prepared:
        raise ValueError("no usable bars were supplied")

    index = panel_index(prepared)
    if len(index) == 0:
        raise ValueError("no bars available")

    # The full pre-start history is *processed* so that positional lookbacks
    # (``iloc[idx - n : idx]``) and indicator windows are warm; results are then
    # trimmed to ``start`` and the equity curve is rebased there. Trades that
    # begin before the window but exit inside it are kept and flagged.
    filtered = index if start is None else index[index >= pd.Timestamp(start)]
    if len(filtered) == 0:
        raise ValueError("no bars remain inside the requested window")
    report_start = pd.Timestamp(start) if start is not None else index[0]

    panels = (
        build_panels(prepared, merged_params, rsi_method=rsi_method)
        if mode == "portfolio"
        else {}
    )

    strategy_params = {**spec_params(spec), **merged_params}
    payload = {
        "strategy_id": strategy_id,
        "params": strategy_params,
        "config": config.as_dict(),
        "interval": interval,
        "mode": mode,
        "symbols": sorted(prepared),
        "start": str(index[0]),
        "end": str(index[-1]),
    }
    fingerprint = _fingerprint(payload)

    slices: list[RunSlice] = []
    if mode == "portfolio":
        slices.append(
            _run_slice(
                strategy_id=strategy_id,
                spec=spec,
                symbols=sorted(prepared),
                label=PORTFOLIO_LABEL,
                bars=prepared,
                panels=panels,
                index=index,
                config=config,
                params=strategy_params,
                mode=mode,
                report_start=report_start,
                interval=interval,
            )
        )
    else:
        for symbol in sorted(prepared):
            single = {symbol: prepared[symbol]}
            symbol_index = pd.DatetimeIndex(prepared[symbol].index)
            if len(symbol_index) == 0:
                continue
            slices.append(
                _run_slice(
                    strategy_id=strategy_id,
                    spec=spec,
                    symbols=[symbol],
                    label=symbol,
                    bars=single,
                    panels=build_panels(single, strategy_params, rsi_method=rsi_method),
                    index=symbol_index,
                    config=config,
                    params=strategy_params,
                    mode=mode,
                    report_start=report_start,
                    interval=interval,
                )
            )

    logger.info(
        "ran %s on %d instrument(s) [%s, %s..%s] fingerprint=%s",
        strategy_id,
        len(slices),
        mode,
        index[0].date(),
        index[-1].date(),
        fingerprint,
    )

    return StrategyRun(
        strategy_id=strategy_id,
        spec=spec,
        params=strategy_params,
        interval=interval,
        mode=mode,
        report_start=report_start,
        report_end=index[-1],
        initial_capital=config.engine.initial_capital,
        slices=slices,
        fingerprint=fingerprint,
        config=config.as_dict(),
    )


def spec_params(spec: StrategySpec) -> dict[str, Any]:
    """Default parameter set declared by the strategy metadata."""
    from nifty_quant.strategies.registry import default_params

    return default_params(spec.strategy_id)
