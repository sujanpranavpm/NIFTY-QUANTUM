"""Signal schemas.

A signal is a research-only statement of what a validated strategy is
expressing at the close of a particular bar. It is *not* an order, and it is
*not* a recommendation: it records entry/exit intent, the price levels that
defined that intent, the validation verdict that gates it, and the provenance
needed to reproduce it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

import pandas as pd


class SignalAction(str, Enum):
    """Discrete actions the engine emits."""

    BUY = "BUY"
    SELL = "SELL"  # short entry, only for long-short strategies
    HOLD = "HOLD"  # long/short position is open and no exit is pending
    EXIT = "EXIT"  # an exit order is pending
    FLAT = "FLAT"  # no position and no pending entry
    BLOCKED = "BLOCKED"  # validation did not approve the strategy


@dataclass(frozen=True)
class Signal:
    """One research-only signal for one strategy × one symbol.

    ``as_of`` is the close of the last bar the strategy was run on. The pending
    orders that produced the signal can only fill from the *next* bar onward,
    so ``earliest_fill_at`` is strictly after ``as_of`` whenever the data
    frequency makes that meaningful.
    """

    strategy_id: str
    symbol: str
    action: SignalAction
    as_of: pd.Timestamp | datetime | None
    generated_at: datetime
    signal_at: pd.Timestamp | datetime | None = None
    earliest_fill_at: pd.Timestamp | datetime | None = None
    reference_price: float | None = None
    entry_price: float | None = None
    stop_loss: float | None = None
    target: float | None = None
    order_type: str = ""
    exit_conditions: list[str] = field(default_factory=list)
    risk_reward: float | None = None
    valid: bool = False
    validity_reason: str = ""
    verdict: str = "untested"
    checks: dict[str, Any] = field(default_factory=dict, repr=False)
    history: dict[str, Any] = field(default_factory=dict, repr=False)
    fingerprint: str = ""
    params: dict[str, Any] = field(default_factory=dict, repr=False)
    position: float = 0.0
    raw_orders: list[dict[str, Any]] = field(default_factory=list, repr=False)

    def to_row(self) -> dict[str, Any]:
        """Flat dictionary for CSV / DataFrame export."""
        return {
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "action": self.action.value,
            "as_of": self.as_of,
            "generated_at": self.generated_at,
            "signal_at": self.signal_at,
            "earliest_fill_at": self.earliest_fill_at,
            "reference_price": self.reference_price,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "target": self.target,
            "order_type": self.order_type,
            "exit_conditions": " | ".join(self.exit_conditions),
            "risk_reward": self.risk_reward,
            "valid": self.valid,
            "validity_reason": self.validity_reason,
            "verdict": self.verdict,
            "fingerprint": self.fingerprint,
            "position": self.position,
        }


def signals_to_frame(signals: list[Signal]) -> pd.DataFrame:
    """Convert a list of signals to a DataFrame, preserving column order."""
    if not signals:
        return pd.DataFrame()
    return pd.DataFrame([s.to_row() for s in signals])
