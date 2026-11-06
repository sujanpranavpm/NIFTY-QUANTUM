"""The common options-buying pipeline that wires every component together.

    daily signal -> contract selection -> OptionBar series -> ExitRules
    -> OptionsExecutionEngine -> IndiaOptionsCommission -> trade records

Design guarantees (each pinned by tests):

* the strategies are *not* modified - signals come from the real engine
  (:func:`nifty_quant.options.signals.index_daily_signals`);
* entry is mapped by timestamp to session *D+1* and the engine keeps its next-bar,
  stop-wins, adverse-gap and UNRESOLVED behaviour (:mod:`nifty_quant.options.bars`);
* MIN_DTE is enforced **at entry** (``D+1``) with expiry-day entries excluded;
* the conservative scenario's liquidity floor is made **volume-unit-aware** by scaling
  it to ``lot_size x lots`` share-equivalents (the preset ``1.0`` is one share, not a
  tradable exit);
* costs are the P2.1.2 schedule through the same :class:`IndiaOptionsCommission`, so
  brokerage is excluded-by-default and every figure carries its verification level.

The exit convention is a **fixed** fraction of the entry premium (not tuned, not
optimised) - it exists to give every candidate identical exit rules, nothing more.
Since P2.2.6.2 the going-forward default is the **approved** +15%/−8% band (with
orders_12's ratio-preserving +16%/−8%) resolved centrally per strategy through
:data:`EXIT_POLICY`; the historical ±30%/30% band is reproducible only via explicit
configuration, never via a default.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional, Sequence

import pandas as pd

from nifty_quant.config import BASELINE, CONSERVATIVE, OPTIMISTIC, ExecutionScenario
from nifty_quant.engine.options_costs import IndiaOptionsCommission, UnknownChargeError
from nifty_quant.engine.options_execution import OptionsExecutionEngine
from nifty_quant.engine.orders import OrderSide
from nifty_quant.options.bars import build_option_bars
from nifty_quant.options.data import MarketData
from nifty_quant.options.records import RecordSet, RejectionRecord, TradeRecord, persist_run
from nifty_quant.options.reference import OptionReference
from nifty_quant.options.selection import select_contract
from nifty_quant.options.signals import index_daily_signals

TIER1 = ("orders_12", "orders_01", "orders_18", "orders_05", "orders_07")


@dataclass(frozen=True)
class ExitConvention:
    """Fixed premium-proportional stop/target - a convention, never a tuned value.

    Defaults are the **approved** band adopted by the user-authorised P2.2.6.2
    decision (TP +15% / SL -8% of entry premium, tick-rounded).  The historical
    +/-30%/30% band used by P2.2.3-P2.2.5 stays reproducible through explicit
    construction (``ExitConvention(0.30, 0.30)``), never through this default.
    """

    stop_fraction: float = 0.08    # stop = entry premium x (1 - 0.08)
    target_fraction: float = 0.15  # target = entry premium x (1 + 0.15)
    max_hold_bars: Optional[int] = None

    def __post_init__(self) -> None:
        # Fail closed on nonsense values (NaN, negatives, non-numbers) instead of
        # silently producing degenerate or unbounded exit rules.
        for name in ("stop_fraction", "target_fraction"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(f"ExitConvention.{name} must be a real number, got {v!r}")
            if not math.isfinite(float(v)) or float(v) < 0.0 or float(v) > 10.0:
                raise ValueError(f"ExitConvention.{name} must be finite in [0, 10], got {v!r}")
        if self.max_hold_bars is not None and int(self.max_hold_bars) <= 0:
            raise ValueError(f"ExitConvention.max_hold_bars must be positive, got {self.max_hold_bars!r}")

    def rules_for(self, entry_open: float, tick: float):
        from nifty_quant.engine.options_execution import ExitRules

        def to_tick(x: float) -> float:
            if tick and tick > 0:
                return round(x / tick) * tick
            return round(x, 2)

        stop = to_tick(entry_open * (1.0 - self.stop_fraction))
        target = to_tick(entry_open * (1.0 + self.target_fraction))
        return ExitRules(stop_premium=stop, target_premium=target,
                         max_hold_bars=self.max_hold_bars)


# ---- approved strategy-specific exit policy (P2.2.6.2, user-authorised) ---------
# Single source of truth for the going-forward default band, so multi-strategy runs
# apply orders_12's ratio-preserving +16%/-8% WITHOUT callers having to remember the
# exception.  +/-30%/30% is NOT part of this policy - it exists only as the pinned
# historical configuration of the P2.2.3/P2.2.4/P2.2.5 experiments.
APPROVED_DEFAULT_EXIT = ExitConvention(stop_fraction=0.08, target_fraction=0.15)
EXIT_POLICY: dict[str, ExitConvention] = {
    "orders_12": ExitConvention(stop_fraction=0.08, target_fraction=0.16),  # 2:1 honoured
    "orders_01": APPROVED_DEFAULT_EXIT,
    "orders_18": APPROVED_DEFAULT_EXIT,
    "orders_05": APPROVED_DEFAULT_EXIT,
    "orders_07": APPROVED_DEFAULT_EXIT,
}


def approved_exit_for(strategy_id: str) -> ExitConvention:
    """Central-policy band for one strategy id; fail-closed on unknown ids."""
    try:
        return EXIT_POLICY[strategy_id]
    except KeyError:
        raise ValueError(
            f"no approved exit policy for strategy {strategy_id!r}; covered ids are "
            f"{sorted(EXIT_POLICY)} - pass an explicit ExitConvention via AdapterConfig("
            f"exit=...) to run a strategy outside the policy") from None


@dataclass(frozen=True)
class AdapterConfig:
    strategies: tuple[str, ...] = TIER1
    resolution: str = "15m"
    sensitivity_resolution: str = "5m"
    min_dte: int = 2
    lots: int = 1
    max_strike_steps: int = 1
    # ``exit=None`` (the default) applies the central per-strategy EXIT_POLICY -
    # the approved band without callers having to special-case orders_12.  An
    # explicit ExitConvention overrides the policy for every selected strategy
    # (historical +/-30% replays and single-band research overrides).
    exit: Optional[ExitConvention] = None
    scenarios: tuple[ExecutionScenario, ...] = (OPTIMISTIC, BASELINE, CONSERVATIVE)

    def exit_for(self, strategy_id: str) -> ExitConvention:
        """The band actually governing this strategy under this config."""
        return self.exit if self.exit is not None else approved_exit_for(strategy_id)

    def scenarios_for(self, lot_size: int, lots: int) -> tuple[ExecutionScenario, ...]:
        """Unit-aware liquidity: scale the conservative floor to lot_size x lots.

        The preset ``min_exit_volume=1.0`` means one *share-equivalent* (the candle
        volume unit), which is not a tradable exit; a full position is ``lot_size x
        lots`` share-equivalents.  Optimistic/baseline keep the gate disabled (0.0).
        """
        out = []
        for sc in self.scenarios:
            if sc.min_exit_volume and sc.min_exit_volume > 0.0:
                out.append(dataclasses.replace(sc, min_exit_volume=float(lot_size) * lots))
            else:
                out.append(sc)
        return tuple(out)


@dataclass
class RunResult:
    records: RecordSet
    summary: dict
    eligible: dict

    @property
    def trades_frame(self):
        return self.records.trades_frame()

    @property
    def rejections_frame(self):
        return self.records.rejections_frame()


class OptionTradeAdapter:
    """Run the common buying pipeline for a set of unchanged strategies."""

    def __init__(self, config: Optional[AdapterConfig] = None, *,
                 market: Optional[MarketData] = None,
                 reference: Optional[OptionReference] = None,
                 commission: Optional[IndiaOptionsCommission] = None) -> None:
        self.config = config or AdapterConfig()
        self.market = market or MarketData()
        self.reference = reference or OptionReference(sessions=self.market.sessions())
        self.commission = commission or IndiaOptionsCommission()
        self.engine = OptionsExecutionEngine(self.commission)
        self._expiries: Optional[list[date]] = None

    @property
    def observed_expiries(self) -> list[date]:
        if self._expiries is None:
            self._expiries = self.market.option_expiries(self.config.resolution)
        return self._expiries

    # ---- opportunity-set statistics (re-derives the reconciliation numbers) ----
    def eligible_entry_stats(self) -> dict:
        sessions = set(self.reference.sessions)
        expiries = self.observed_expiries
        day_sets: dict[date, set[date]] = {}
        opt_days: set[date] = set()
        for expiry in expiries:
            frame = self.market.option_frame(expiry, self.config.resolution)
            days = ({date.fromisoformat(str(x)[:10]) for x in frame["trading_day"].unique()}
                    if frame is not None and len(frame) else set())
            day_sets[expiry] = days
            opt_days |= days
        overlap = sorted(d for d in opt_days if d in sessions)

        def max_dte(day: date) -> int:
            return max((self.reference.dte(day, x) for x in expiries if day in day_sets[x]),
                       default=-1)

        ladder = {f"dte_ge_{thr}": sum(1 for e in overlap if max_dte(e) >= thr)
                  for thr in (0, 1, 2, 3)}
        eligible = [e for e in overlap
                    if self.reference.previous_session(e) in sessions
                    and max_dte(e) >= self.config.min_dte]
        return {
            "resolution": self.config.resolution,
            "usable_expiry_files": len(expiries),
            "distinct_option_days": len(opt_days),
            "index_sessions": len(sessions),
            "overlap_option_days": len(overlap),
            "dte_ladder_overlap_days": ladder,
            "min_dte_at_entry": self.config.min_dte,
            "eligible_entry_days": len(eligible),
        }

    # ---- one signal -> one (attempted) trade per scenario ----
    def _record_signal(self, sid: str, signal, records: RecordSet) -> None:
        entry_day = self.reference.next_session(signal.day)
        if entry_day is None:
            records.rejections.append(RejectionRecord(
                sid, str(signal.day), None, signal.direction, signal.spot,
                "NO_NEXT_SESSION", "selection"))
            return

        sel = select_contract(
            entry_day, signal.spot, signal.direction,
            ref=self.reference, market=self.market,
            observed_expiries=self.observed_expiries,
            min_dte=self.config.min_dte, max_strike_steps=self.config.max_strike_steps,
            resolution=self.config.resolution)
        if not sel.ok:
            records.rejections.append(RejectionRecord(
                sid, str(signal.day), str(entry_day), signal.direction, signal.spot,
                sel.reason or "SELECTION_FAILED", "selection",
                {"strike_target": sel.strike_target}))
            return
        if sel.dte_at_entry < self.config.min_dte:  # belt-and-braces (sel already filters)
            records.rejections.append(RejectionRecord(
                sid, str(signal.day), str(entry_day), signal.direction, signal.spot,
                "DTE_BELOW_MIN", "selection", {"dte": sel.dte_at_entry}))
            return
        if sel.lot_size <= 0:
            records.rejections.append(RejectionRecord(
                sid, str(signal.day), str(entry_day), signal.direction, signal.spot,
                "LOT_UNKNOWN", "selection"))
            return

        frame = self.market.option_frame(sel.expiry, self.config.resolution)
        series = build_option_bars(frame, strike=sel.strike, option_type=sel.option_type,
                                   entry_day=entry_day, expiry=sel.expiry)
        if not series.ok:
            records.rejections.append(RejectionRecord(
                sid, str(signal.day), str(entry_day), signal.direction, signal.spot,
                series.reason or "BAR_BUILD_FAILED", "bars",
                {"symbol": sel.symbol}))
            return

        entry_open = series.entry_open
        rules = self.config.exit_for(sid).rules_for(entry_open, sel.tick_size)
        scenarios = self.config.scenarios_for(sel.lot_size, self.config.lots)
        for sc in scenarios:
            try:
                tr = self.engine.run(series.bars, series.signal_index, rules,
                                     lots=self.config.lots, lot_size=sel.lot_size, scenario=sc)
            except Exception as exc:  # noqa: BLE001 - never fabricate a fill on failure
                records.rejections.append(RejectionRecord(
                    sid, str(signal.day), str(entry_day), signal.direction, signal.spot,
                    f"EXECUTION_ERROR:{type(exc).__name__}", "execution", {"symbol": sel.symbol}))
                continue
            self._record_trade(sid, signal, entry_day, sel, series, tr, records)

    def _record_trade(self, sid, signal, entry_day, sel, series, tr, records) -> None:
        exit_date = (str(pd.Timestamp(tr.exit_timestamp).date())
                     if tr.exit_timestamp is not None else None)
        verify = self._verify(sel, tr, entry_day, exit_date)
        entry_ts = tr.entry_timestamp
        records.trades.append(TradeRecord(
            strategy_id=sid,
            direction=sel.direction,
            option_type=sel.option_type,
            symbol=sel.symbol,
            strike=sel.strike,
            expiry=str(sel.expiry),
            dte_at_entry=sel.dte_at_entry,
            moneyness=sel.moneyness,
            signal_date=str(signal.day),
            entry_date=str(date.fromisoformat(str(pd.Timestamp(entry_ts).date()))) if entry_ts is not None else str(entry_day),
            exit_date=exit_date,
            lot_size=tr.lot_size,
            lots=tr.lots,
            scenario=tr.scenario,
            status=tr.status,
            exit_reason=tr.reason,
            bars_held=tr.bars_held,
            entry_premium=tr.entry_premium,
            exit_premium=tr.exit_premium,
            gross_pnl=tr.gross_pnl,
            total_costs=tr.total_costs,
            net_pnl=tr.net_pnl,
            statutory_verified=verify["statutory_verified"],
            all_in_verified=verify["all_in_verified"],
            brokerage_excluded=verify["brokerage_excluded"],
            cost_basis=verify["cost_basis"],
            fallback_used=sel.fallback_used,
            tick_size=sel.tick_size,
            strike_interval=sel.strike_interval,
        ))

    def _verify(self, sel, tr, entry_day, exit_date) -> dict:
        """Cost-verification level for this trade (P2.1.2 discipline preserved)."""
        try:
            if tr.exit_premium is not None and exit_date is not None:
                rt = self.commission.round_trip(tr.entry_premium, tr.exit_premium,
                                                tr.lot_size, tr.lots, str(entry_day), exit_date)
                return {"statutory_verified": rt["statutory_verified"],
                        "all_in_verified": rt["all_in_verified"],
                        "brokerage_excluded": rt["brokerage_excluded"],
                        "cost_basis": rt["cost_basis"]}
            entry = self.commission.assess(tr.entry_premium, tr.lot_size, tr.lots,
                                           OrderSide.BUY, str(entry_day))
            return {"statutory_verified": entry.statutory_verified,
                    "all_in_verified": entry.all_in_verified,
                    "brokerage_excluded": entry.brokerage_excluded,
                    "cost_basis": entry.cost_basis}
        except UnknownChargeError as exc:
            return {"statutory_verified": False, "all_in_verified": False,
                    "brokerage_excluded": True,
                    "cost_basis": f"unverified: no effective-dated rate ({exc})"}

    # ---- public run -------------------------------------------------------
    def run(self, *, strategies: Optional[Sequence[str]] = None,
            persist: bool = False, out_dir: Optional[Path] = None) -> RunResult:
        index_daily = self.market.index_daily()
        records = RecordSet()
        per_strategy: dict[str, dict] = {}
        chosen = tuple(strategies or self.config.strategies)
        if not chosen:
            raise ValueError("AdapterConfig.strategies resolved to the empty set; "
                             "at least one strategy id is required")

        for sid in chosen:
            before = (len(records.trades), len(records.rejections))
            for signal in index_daily_signals(sid, index_daily):
                self._record_signal(sid, signal, records)
            per_strategy[sid] = {
                "trades": len(records.trades) - before[0],
                "rejections": len(records.rejections) - before[1],
            }

        # exit bands actually applied, per strategy (policy resolution or explicit
        # override) - recorded so every future run is self-documenting.
        exit_used = {sid: dataclasses.asdict(self.config.exit_for(sid)) for sid in chosen}
        uniform = list({tuple(sorted(v.items())) for v in exit_used.values()})
        summary = {
            "strategies": list(chosen),
            "resolution": self.config.resolution,
            "min_dte_at_entry": self.config.min_dte,
            "lots_per_trade": self.config.lots,
            "exit_convention": (exit_used[chosen[0]] if len(uniform) == 1
                                else {"per_strategy": exit_used}),
            "exit_convention_per_strategy": exit_used,
            "exit_policy_applied": self.config.exit is None,
            "slippage_bps_note": "0/2/5 bps are ESTIMATED execution assumptions, not measured fills",
            "per_strategy": per_strategy,
        }
        result = RunResult(records=records, summary=summary,
                           eligible=self.eligible_entry_stats())
        if persist and out_dir is not None:
            result.summary["paths"] = persist_run(records, out_dir, summary=summary)
        return result
