"""Verdict-gated signal engine.

The engine does not look at prices directly and does not implement its own
logic. It reruns the selected strategies over the latest available bars,
captures the orders that are pending at the close of the last bar, and turns
those orders into research signals. If a strategy failed validation, every
signal it could have produced is replaced with ``BLOCKED``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import pandas as pd

import hashlib
import json

from nifty_quant.config import ResearchConfig
from nifty_quant.engine.backtest import StrategyRun, run_backtest
from nifty_quant.signals.gating import DEFAULT_APPROVAL_CRITERIA, approved_strategies
from nifty_quant.signals.parsing import parse_slice
from nifty_quant.signals.schemas import Signal, SignalAction, signals_to_frame
from nifty_quant.strategies.registry import default_params, spec as get_spec
from nifty_quant.validation.report import ValidationReport

LOG = logging.getLogger(__name__)

DEFAULT_LOOKBACK_BARS = 750


class SignalEngine:
    """Generate signals from a validation report and a bar universe."""

    def __init__(
        self,
        validation: ValidationReport,
        *,
        config: ResearchConfig | None = None,
        lookback_bars: int = DEFAULT_LOOKBACK_BARS,
        interval: str = "1d",
        approval_criteria: dict[str, Any] | None = None,
    ) -> None:
        if validation is None or validation.verdicts.empty:
            raise ValueError("a validation report with verdicts is required; signal generation is gated")
        self.validation = validation
        self.config = config or ResearchConfig()
        self.lookback_bars = max(1, int(lookback_bars))
        self.interval = interval
        self.approval = approved_strategies(validation, criteria=approval_criteria)

    @property
    def approved_strategy_ids(self) -> list[str]:
        return sorted(sid for sid, info in self.approval.items() if info.get("approved"))

    @property
    def rejected_strategy_ids(self) -> list[str]:
        return sorted(sid for sid, info in self.approval.items() if not info.get("approved"))

    def generate(
        self,
        bars: dict[str, pd.DataFrame],
        *,
        symbols: list[str] | None = None,
        as_of: pd.Timestamp | str | None = None,
    ) -> list[Signal]:
        """Run every strategy and emit one signal per (strategy, symbol).

        Approved strategies are rerun and their pending orders are parsed.
        Rejected strategies emit ``BLOCKED`` without being rerun, because the
        validation gate has already decided they must not influence positions.

        Args:
            bars: symbol -> OHLCV frame, exactly as accepted by ``run_backtest``.
            symbols: subset to evaluate; defaults to every symbol in ``bars``.
            as_of: optional timestamp to use as the signal bar. If omitted the
                engine uses the latest common timestamp among the supplied bars.
        """
        wanted = list(symbols) if symbols is not None else sorted(bars)
        available = {s: bars[s] for s in wanted if s in bars and bars[s] is not None and not bars[s].empty}
        if not available:
            LOG.warning("no bars available for signal generation")
            return []

        reference_index = self._common_index(available)
        if reference_index.empty:
            LOG.warning("supplied bars share no common dates")
            return []
        latest = reference_index[-1]
        if as_of is not None:
            latest = min(pd.Timestamp(as_of), latest)

        reference_prices = self._last_prices(available, latest)
        signals: list[Signal] = []

        for strategy_id, info in self.approval.items():
            if not info.get("approved"):
                signals.extend(
                    self._blocked_signals(strategy_id, info, available, latest, reference_prices)
                )
                continue
            try:
                run = self._run_strategy(strategy_id, available, latest)
            except Exception as exc:  # a strategy may legitimately refuse
                LOG.warning("signal run failed for %s: %s", strategy_id, exc)
                continue
            if run is None:
                continue
            signals.extend(
                self._signals_from_run(
                    run,
                    approval=info,
                    latest=latest,
                    reference_prices=reference_prices,
                )
            )
        LOG.info(
            "generated %d signals (%d approved, %d blocked)",
            len(signals),
            len([s for s in signals if s.valid]),
            len([s for s in signals if not s.valid]),
        )
        return signals

    def generate_blocked(
        self,
        bars: dict[str, pd.DataFrame],
        *,
        symbols: list[str] | None = None,
        as_of: pd.Timestamp | str | None = None,
    ) -> list[Signal]:
        """Emit BLOCKED signals for rejected strategies without running them."""
        wanted = list(symbols) if symbols is not None else sorted(bars)
        available = {s: bars[s] for s in wanted if s in bars and bars[s] is not None and not bars[s].empty}
        if not available:
            return []
        reference_index = self._common_index(available)
        latest = reference_index[-1] if not reference_index.empty else pd.Timestamp(as_of) if as_of else pd.Timestamp.now()
        if as_of is not None:
            latest = min(pd.Timestamp(as_of), latest)
        reference_prices = self._last_prices(available, latest)

        signals: list[Signal] = []
        for strategy_id, info in self.approval.items():
            if info.get("approved"):
                continue
            signals.extend(
                self._blocked_signals(strategy_id, info, available, latest, reference_prices)
            )
        return signals

    def to_frame(self, signals: list[Signal] | None = None) -> pd.DataFrame:
        if signals is None:
            return pd.DataFrame()
        return signals_to_frame(signals)

    def summary(self, signals: list[Signal]) -> dict[str, Any]:
        frame = self.to_frame(signals)
        if frame.empty:
            return {"total": 0, "approved": 0, "blocked": 0, "actions": {}}
        return {
            "total": len(frame),
            "approved": int(frame["valid"].sum()),
            "blocked": int((~frame["valid"]).sum()),
            "actions": frame["action"].value_counts().to_dict(),
            "symbols": sorted(frame["symbol"].unique()),
            "strategies": sorted(frame["strategy_id"].unique()),
        }

    # ----------------------------------------------------------------------
    # Internals
    # ----------------------------------------------------------------------
    def _run_strategy(
        self,
        strategy_id: str,
        bars: dict[str, pd.DataFrame],
        latest: pd.Timestamp,
    ) -> StrategyRun | None:
        spec = get_spec(strategy_id)
        start = self._start_for_latest(bars, latest)
        if start is None:
            return None
        if spec.run_mode == "portfolio":
            return run_backtest(
                strategy_id,
                bars,
                config=self.config,
                start=start,
                interval=self.interval,
            )
        # Per-stock mode: one run per symbol, exactly like the research runner.
        slices: list = []
        for symbol in sorted(bars):
            single = {symbol: bars[symbol]}
            try:
                run = run_backtest(
                    strategy_id,
                    single,
                    config=self.config,
                    start=start,
                    interval=self.interval,
                )
            except Exception as exc:
                LOG.warning("signal run failed for %s/%s: %s", strategy_id, symbol, exc)
                continue
            slices.extend(run.slices)
        if not slices:
            return None
        params = {**default_params(strategy_id), **dict(spec.params or {})}
        fingerprint = self._fingerprint(
            strategy_id=strategy_id,
            params=params,
            symbols=sorted(bars),
            start=start,
            end=latest,
        )
        return StrategyRun(
            strategy_id=strategy_id,
            spec=spec,
            params=params,
            interval=self.interval,
            mode=spec.run_mode,
            report_start=start,
            report_end=latest,
            initial_capital=self.config.engine.initial_capital,
            slices=slices,
            fingerprint=fingerprint,
        )

    def _signals_from_run(
        self,
        run: StrategyRun,
        *,
        approval: dict[str, Any],
        latest: pd.Timestamp,
        reference_prices: dict[str, float],
    ) -> list[Signal]:
        spec = run.spec
        history = {
            "interval": run.interval,
            "mode": run.mode,
            "fingerprint": run.fingerprint,
            "report_start": str(run.report_start) if run.report_start else None,
            "report_end": str(run.report_end) if run.report_end else None,
        }
        signals: list[Signal] = []
        for run_slice in run.slices:
            signals.extend(
                parse_slice(
                    strategy_id=run.strategy_id,
                    run_slice=run_slice,
                    approval=approval,
                    direction=spec.direction,
                    fingerprint=run.fingerprint,
                    params=run.params,
                    history=history,
                    reference_prices=reference_prices,
                    as_of=latest,
                    earliest_fill_at=self._next_bar(latest),
                )
            )
        return signals

    def _blocked_signals(
        self,
        strategy_id: str,
        info: dict[str, Any],
        bars: dict[str, pd.DataFrame],
        latest: pd.Timestamp,
        reference_prices: dict[str, float],
    ) -> list[Signal]:
        symbols = sorted(bars)
        return [
            Signal(
                strategy_id=strategy_id,
                symbol=symbol,
                action=SignalAction.BLOCKED,
                as_of=latest,
                generated_at=pd.Timestamp.now(),
                signal_at=latest,
                earliest_fill_at=self._next_bar(latest),
                reference_price=reference_prices.get(symbol),
                valid=False,
                validity_reason=info.get("reason", "validation did not approve this strategy"),
                verdict=str(info.get("verdict", "untested")),
                checks=dict(info.get("checks", {})),
                history={},
                fingerprint="",
                params={},
                position=0.0,
            )
            for symbol in symbols
        ]

    def _fingerprint(
        self,
        *,
        strategy_id: str,
        params: dict[str, Any],
        symbols: list[str],
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> str:
        payload = {
            "strategy_id": strategy_id,
            "params": params,
            "config": self.config.as_dict(),
            "interval": self.interval,
            "symbols": symbols,
            "start": str(start),
            "end": str(end),
        }
        blob = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def _start_for_latest(self, bars: dict[str, pd.DataFrame], latest: pd.Timestamp) -> pd.Timestamp | None:
        """Choose a recent start while keeping enough history for warm-up."""
        # Use the symbol with the longest history so the start is feasible.
        longest = max(bars.values(), key=len)
        index = pd.DatetimeIndex(longest.index).sort_values()
        if index.empty:
            return None
        # Ensure we include the requested latest bar.
        if latest < index[0]:
            return index[0]
        if latest > index[-1]:
            latest = index[-1]
        # Locate the bar ``lookback_bars`` before the latest one, then walk
        # back to the earliest available history if necessary.
        pos = index.get_indexer([latest], method="nearest")[0]
        start_pos = max(0, pos - self.lookback_bars + 1)
        return index[start_pos]

    def _common_index(self, bars: dict[str, pd.DataFrame]) -> pd.DatetimeIndex:
        indices = [pd.DatetimeIndex(f.index) for f in bars.values()]
        common = indices[0]
        for idx in indices[1:]:
            common = common.intersection(idx)
        return common.sort_values()

    def _last_prices(self, bars: dict[str, pd.DataFrame], latest: pd.Timestamp) -> dict[str, float]:
        out: dict[str, float] = {}
        for symbol, frame in bars.items():
            if frame is None or frame.empty or "close" not in frame.columns:
                continue
            try:
                value = frame.loc[:latest, "close"].iloc[-1]
                if pd.notna(value):
                    out[symbol] = float(value)
            except Exception:
                continue
        return out

    def _next_bar(self, timestamp: pd.Timestamp) -> pd.Timestamp | None:
        """The earliest time at which the pending order could fill.

        For daily data this is the next trading day; we do not know the calendar
        here, so we advance one calendar day and let the consumer align it.
        """
        if timestamp is None:
            return None
        try:
            return timestamp + pd.Timedelta(days=1)
        except Exception:
            return None
