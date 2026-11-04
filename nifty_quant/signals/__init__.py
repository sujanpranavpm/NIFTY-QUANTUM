"""Verdict-gated signal engine.

The engine reruns validated strategies on the latest available bars and turns
the pending orders captured at the close of the last bar into research-only
signals. Strategies that failed validation emit ``BLOCKED``; no strategy can
bypass the gate through configuration.
"""

from nifty_quant.signals.engine import SignalEngine
from nifty_quant.signals.gating import DEFAULT_APPROVAL_CRITERIA, approved_strategies
from nifty_quant.signals.io import save_signals, signals_by_strategy
from nifty_quant.signals.schemas import Signal, SignalAction, signals_to_frame

__all__ = [
    "DEFAULT_APPROVAL_CRITERIA",
    "Signal",
    "SignalAction",
    "SignalEngine",
    "approved_strategies",
    "save_signals",
    "signals_by_strategy",
    "signals_to_frame",
]
