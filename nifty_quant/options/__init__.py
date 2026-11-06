"""NIFTY options-buying research adapter (P2.2.3).

A single, common pipeline that turns an *unchanged* daily equity strategy into a
realistic, reproducible NIFTY index-**option buying** experiment:

    daily NIFTY signal -> deterministic CE/PE selection -> strike & expiry
    selection -> 15-minute option premium bars -> premium-based ExitRules
    -> :class:`~nifty_quant.engine.options_execution.OptionsExecutionEngine`
    -> :class:`~nifty_quant.engine.options_costs.IndiaOptionsCommission`
    -> per-trade records (+ every rejected signal and its reason).

Nothing here reimplements a strategy or invents a fill: the strategies run through
the real engine (:func:`nifty_quant.engine.backtest.run_backtest`), execution reuses
:class:`OptionsExecutionEngine` (next-bar, stop-wins, adverse-gap, UNRESOLVED), and
costing reuses the P2.1.2 effective-dated schedule.  Every module is a *wiring*
specification over components that already exist and are pinned by the test suite.

The package is intentionally free of optimisation, winner-picking and live/broker
integration - those are out of scope for P2.2.3.
"""

from __future__ import annotations

from nifty_quant.options.bars import BarSeries, build_option_bars
from nifty_quant.options.data import MarketData
from nifty_quant.options.pipeline import (
    APPROVED_DEFAULT_EXIT, EXIT_POLICY, AdapterConfig, ExitConvention,
    OptionTradeAdapter, RunResult, approved_exit_for)
from nifty_quant.options.records import RecordSet, RejectionRecord, TradeRecord, persist_run
from nifty_quant.options.reference import EffectiveLookup, OptionReference
from nifty_quant.options.selection import ContractSelection, select_contract
from nifty_quant.options.signals import DailySignal, index_daily_signals

__all__ = [
    "APPROVED_DEFAULT_EXIT",
    "AdapterConfig",
    "BarSeries",
    "ContractSelection",
    "DailySignal",
    "EffectiveLookup",
    "EXIT_POLICY",
    "ExitConvention",
    "MarketData",
    "OptionReference",
    "OptionTradeAdapter",
    "RecordSet",
    "RejectionRecord",
    "RunResult",
    "TradeRecord",
    "approved_exit_for",
    "build_option_bars",
    "index_daily_signals",
    "persist_run",
    "select_contract",
]
