"""Options execution simulation (P2 stage 6, directive Part 7).

A *long* index-option trade (buy CE or buy PE) is simulated against a sequence of
:class:`OptionBar` candles with the same anti-look-ahead discipline the equity
:class:`~nifty_quant.engine.fill_engine.FillEngine` is pinned to, re-expressed for a
premium-based contract:

* **Next-bar entry.** A signal raised on bar *i* never fills on bar *i*.  The earliest
  execution is bar *i+1*'s open - there is no automatic entry at the signal-candle close.
* **Next-bar exit.** Protective/limit exits are only evaluated from the bar *after* the
  entry fill, so a position can never be entered and left inside one candle.
* **Conservative collision.** If a bar could touch both the stop and the target, the
  **stop wins** - the low assumption for a long position, identical across every scenario.
* **Gap handling.** A stop that gaps through fills at the worse price (``min(stop, open)``
  for a sell), never at the stop price the strategy hoped for.
* **Missing-data integrity.** A bar flagged ``available=False`` is an interval we cannot
  observe, so no fill is ever assumed inside it.  If expiry arrives on a missing bar the
  trade is returned :data:`UNRESOLVED` rather than given a fabricated settlement price.
* **Liquidity gate.** Where a scenario sets a floor, an exit whose bar volume is below it
  is *rejected* (the position stays open), never silently traded at an untradable size.
* **No claim of exchange execution.** Every figure is a simulation; the result carries the
  scenario name and the cost engine's strict-verifiability verdict so a reader can see how
  much of the number is assumption.

The three named scenarios (:data:`~nifty_quant.config.OPTIMISTIC` / BASELINE /
CONSERVATIVE) differ only in fill pricing (adverse slippage) and the liquidity floor, so
running one trade through all three yields a profitability *spread*.  A result that only
pays under the optimistic scenario is, by construction, visible as fragile.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from nifty_quant.config import ExecutionScenario
from nifty_quant.engine.options_costs import IndiaOptionsCommission
from nifty_quant.engine.orders import OrderSide

# ---- result status vocabulary ------------------------------------------------
FILLED = "FILLED"
NO_ENTRY = "NO_ENTRY"
UNRESOLVED = "UNRESOLVED"
OPEN_AT_END = "OPEN_AT_END"

# ---- exit reasons -------------------------------------------------------------
REASON_STOP = "STOP_LOSS"
REASON_TARGET = "TAKE_PROFIT"
REASON_TRAIL = "TRAILING_STOP"
REASON_TIME = "TIME_EXIT"
REASON_EXPIRY = "EXPIRY_SETTLE"
REASON_END = "END_OF_DATA"


def _valid(value: object) -> bool:
    try:
        numeric = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(numeric)


def _day(timestamp: Any) -> str:
    """A bare ISO date for the effective-dated cost schedule, from any stamp type."""
    date_part = getattr(timestamp, "date", None)
    if callable(date_part):
        return date_part().isoformat()
    return str(timestamp)[:10]


@dataclass(frozen=True)
class OptionBar:
    """One observable premium candle for an option contract.

    ``available`` is the honest completeness flag: a bar we cannot observe carries no
    usable prices and forbids any fill, so a gap is never traded through.
    """

    timestamp: Any
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    available: bool = True
    is_expiry: bool = False

    def __post_init__(self) -> None:
        if not self.available:
            return
        for name in ("open", "high", "low", "close"):
            value = getattr(self, name)
            if not _valid(value) or float(value) <= 0.0:
                raise ValueError(f"available bar has a non-positive/non-finite {name}: {value!r}")
        top = max(self.open, self.high, self.low, self.close)
        bottom = min(self.open, self.high, self.low, self.close)
        if self.high < top - 1e-9 or self.low > bottom + 1e-9:
            raise ValueError(f"bar violates OHLC containment at {self.timestamp!r}")


@dataclass(frozen=True)
class ExitRules:
    """Static and dynamic exit conditions for a long option position.

    A trailing stop anchors on the best *prior* bar high (never the current bar), so the
    decision to leave is made only from information available before the bar being traded.
    """

    stop_premium: float | None = None
    target_premium: float | None = None
    trail_amount: float | None = None
    trail_percent: float | None = None
    max_hold_bars: int | None = None

    def __post_init__(self) -> None:
        for name in ("stop_premium", "target_premium", "trail_amount"):
            value = getattr(self, name)
            if value is not None and (not _valid(value) or float(value) < 0.0):
                raise ValueError(f"{name} must be non-negative and finite: {value!r}")
        if self.trail_percent is not None and not (0.0 <= float(self.trail_percent) < 1.0):
            raise ValueError(f"trail_percent must be in [0,1): {self.trail_percent!r}")
        if self.max_hold_bars is not None and int(self.max_hold_bars) <= 0:
            raise ValueError(f"max_hold_bars must be positive: {self.max_hold_bars!r}")


@dataclass(frozen=True)
class TradeResult:
    """The outcome of one simulated option trade under one scenario."""

    scenario: str
    status: str
    reason: str | None
    lots: int
    lot_size: int
    entry_timestamp: Any = None
    entry_premium: float | None = None
    exit_timestamp: Any = None
    exit_premium: float | None = None
    bars_held: int = 0
    gross_pnl: float = 0.0
    entry_costs: float = 0.0
    exit_costs: float = 0.0
    total_costs: float = 0.0
    net_pnl: float = 0.0
    premium_per_lot: float = 0.0
    strict_verifiable: bool = True

    @property
    def entered(self) -> bool:
        return self.entry_premium is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "status": self.status,
            "reason": self.reason,
            "entered": self.entered,
            "entry_premium": self.entry_premium,
            "exit_premium": self.exit_premium,
            "bars_held": self.bars_held,
            "gross_pnl": self.gross_pnl,
            "total_costs": self.total_costs,
            "net_pnl": self.net_pnl,
            "strict_verifiable": self.strict_verifiable,
        }


class OptionsExecutionEngine:
    """Simulate long CE/PE entries and exits over premium bars for a scenario."""

    def __init__(self, commission: IndiaOptionsCommission | None = None) -> None:
        self.commission = commission

    # ---- public API ----------------------------------------------------------
    def run(
        self,
        bars: Sequence[OptionBar],
        signal_index: int,
        rules: ExitRules,
        *,
        lots: int,
        lot_size: int,
        scenario: ExecutionScenario,
    ) -> TradeResult:
        if lots <= 0:
            raise ValueError(f"lots must be positive: {lots!r}")
        if lot_size <= 0:
            raise ValueError(f"lot_size must be positive: {lot_size!r}")
        if not (0 <= signal_index < len(bars)):
            raise ValueError(f"signal_index out of range: {signal_index}")

        # Next-bar entry: a signal on bar i cannot fill until bar i+1's open.
        entry_index = signal_index + 1
        if entry_index >= len(bars):
            return TradeResult(scenario.name, NO_ENTRY, None, lots, lot_size)
        entry_bar = bars[entry_index]
        if not entry_bar.available:
            # Reject an entry into a missing interval; never fabricate a fill price.
            return TradeResult(scenario.name, NO_ENTRY, None, lots, lot_size)

        entry_premium = self._adverse(entry_bar.open, scenario, OrderSide.BUY)
        entry_costs, entry_strict = self._costs(
            entry_premium, lot_size, lots, OrderSide.BUY, _day(entry_bar.timestamp))

        anchor = entry_bar.high  # best observed since entry; used by the trailing stop

        exit_index = None
        exit_bar = None
        exit_price = None
        reason = None
        for j in range(entry_index + 1, len(bars)):
            bar = bars[j]
            held = j - entry_index

            if not bar.available:
                if bar.is_expiry:
                    # Expiry on an unobservable bar: no fabricated settlement price.
                    return self._open_result(
                        scenario, UNRESOLVED, lots, lot_size,
                        entry_bar, entry_premium, entry_costs, entry_strict, held,
                        mark=None, reason=REASON_EXPIRY)
                continue  # nothing can fill inside a gap

            eff_stop = self._effective_stop(rules, anchor)

            stop_hit = eff_stop is not None and bar.low <= eff_stop
            target_hit = rules.target_premium is not None and bar.high >= rules.target_premium
            is_expiry = bar.is_expiry
            time_up = rules.max_hold_bars is not None and held >= rules.max_hold_bars

            # Decision uses only bars strictly before `bar`, then we refresh the anchor.
            if stop_hit:  # stop wins over target on collision (conservative, every scenario)
                exit_index, exit_bar = j, bar
                exit_price = self._adverse(min(eff_stop, bar.open), scenario, OrderSide.SELL)
                reason = REASON_TRAIL if self._is_trailing(rules) else REASON_STOP
            elif target_hit:
                exit_index, exit_bar = j, bar
                # a sell limit fills at the limit price; no favourable gap-up credited
                exit_price = self._adverse(rules.target_premium, scenario, OrderSide.SELL)
                reason = REASON_TARGET
            elif is_expiry:
                exit_index, exit_bar = j, bar
                exit_price = self._adverse(bar.close, scenario, OrderSide.SELL)
                reason = REASON_EXPIRY
            elif time_up:
                exit_index, exit_bar = j, bar
                exit_price = self._adverse(bar.close, scenario, OrderSide.SELL)
                reason = REASON_TIME
            else:
                anchor = max(anchor, bar.high)
                continue

            # Liquidity gate: below the floor we refuse the exit (position stays open).
            if scenario.min_exit_volume > 0.0 and bar.volume < scenario.min_exit_volume:
                anchor = max(anchor, bar.high)
                exit_index = exit_bar = exit_price = reason = None
                continue
            break

        if exit_index is None:
            last = bars[-1]
            mark = last.close if last.available else None
            return self._open_result(
                scenario, OPEN_AT_END, lots, lot_size,
                entry_bar, entry_premium, entry_costs, entry_strict,
                len(bars) - 1 - entry_index, mark=mark, reason=REASON_END)

        exit_costs, exit_strict = self._costs(
            exit_price, lot_size, lots, OrderSide.SELL, _day(exit_bar.timestamp))
        gross = (exit_price - entry_premium) * lot_size * lots
        total = entry_costs + exit_costs
        return TradeResult(
            scenario=scenario.name,
            status=FILLED,
            reason=reason,
            lots=lots,
            lot_size=lot_size,
            entry_timestamp=entry_bar.timestamp,
            entry_premium=entry_premium,
            exit_timestamp=exit_bar.timestamp,
            exit_premium=exit_price,
            bars_held=exit_index - entry_index,
            gross_pnl=gross,
            entry_costs=entry_costs,
            exit_costs=exit_costs,
            total_costs=total,
            net_pnl=gross - total,
            premium_per_lot=(exit_price - entry_premium) * lot_size,
            strict_verifiable=entry_strict and exit_strict,
        )

    def run_scenarios(
        self,
        bars: Sequence[OptionBar],
        signal_index: int,
        rules: ExitRules,
        *,
        lots: int,
        lot_size: int,
        scenarios: Iterable[ExecutionScenario],
    ) -> dict[str, TradeResult]:
        return {
            sc.name: self.run(bars, signal_index, rules,
                              lots=lots, lot_size=lot_size, scenario=sc)
            for sc in scenarios
        }

    # ---- helpers -------------------------------------------------------------
    def _effective_stop(self, rules: ExitRules, anchor: float) -> float | None:
        if rules.trail_amount is not None:
            return anchor - rules.trail_amount
        if rules.trail_percent is not None:
            return anchor * (1.0 - rules.trail_percent)
        return rules.stop_premium

    @staticmethod
    def _is_trailing(rules: ExitRules) -> bool:
        return rules.trail_amount is not None or rules.trail_percent is not None

    def _adverse(self, price: float, scenario: ExecutionScenario, side: OrderSide) -> float:
        """Shift a fill against the trader by the scenario's adverse slippage."""
        factor = scenario.adverse_slippage_bps / 10_000.0
        return price * (1.0 + factor) if side == OrderSide.BUY else price * (1.0 - factor)

    def _costs(self, premium: float, lot_size: int, lots: int,
               side: OrderSide, on_date: str) -> tuple[float, bool]:
        if self.commission is None:
            return 0.0, True
        result = self.commission.assess(premium, lot_size, lots, side, on_date)
        return result.total, result.strict_verifiable

    def _open_result(self, scenario, status, lots, lot_size, entry_bar,
                     entry_premium, entry_costs, entry_strict, bars_held, *,
                     mark, reason):  # noqa: ANN001 - convenience internal constructor
        gross = 0.0 if mark is None else (mark - entry_premium) * lot_size * lots
        return TradeResult(
            scenario=scenario.name,
            status=status,
            reason=reason,
            lots=lots,
            lot_size=lot_size,
            entry_timestamp=entry_bar.timestamp,
            entry_premium=entry_premium,
            exit_timestamp=None,
            exit_premium=None,
            bars_held=bars_held,
            gross_pnl=gross,
            entry_costs=entry_costs,
            exit_costs=0.0,
            total_costs=entry_costs,
            net_pnl=gross - entry_costs,
            premium_per_lot=0.0 if mark is None else (mark - entry_premium) * lot_size,
            strict_verifiable=entry_strict,
        )
