"""Contract specifications for the two non-equity upstream examples.

Upstream obtains these from its ``/bt/prepare`` service: each instrument carries
``multiplier``, ``lot_size`` and (for futures) ``margin``. Those fields drive
position sizing only; the upstream accounting still charges full notional, which
upstream documents as a known limitation.

We do not model FX or futures instruments anywhere else in this engine, so the
specs must be supplied explicitly. Absent a spec the strategies refuse to trade
and say why, instead of silently inventing a contract size.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np
import pandas as pd


class ContractSpecRequired(RuntimeError):
    """Raised when a contract-based strategy has no instrument specification."""


@dataclass(frozen=True)
class ContractSpec:
    """Sizing metadata for a non-equity instrument."""

    multiplier: float = 1.0
    lot_size: float = 1.0
    margin: float = float("nan")

    @classmethod
    def coerce(cls, value: "ContractSpec | Mapping[str, Any]") -> "ContractSpec":
        if isinstance(value, cls):
            return value
        return cls(
            multiplier=float(value.get("multiplier", 1.0)),
            lot_size=float(value.get("lot_size", 1.0)),
            margin=float(value.get("margin", float("nan"))),
        )


def resolve_contract_spec(
    specs: Mapping[str, "ContractSpec | Mapping[str, Any]"] | None,
    instrument: str,
) -> ContractSpec:
    """Look up one instrument's spec, refusing to guess when it is absent."""
    if not specs or instrument not in specs:
        raise ContractSpecRequired(
            f"no contract spec for {instrument!r}; supply contract_specs with "
            "multiplier/lot_size (and margin for futures) before running this strategy"
        )
    return ContractSpec.coerce(specs[instrument])


def average_true_range(
    high: pd.DataFrame,
    low: pd.DataFrame,
    close: pd.DataFrame,
    window: int = 20,
) -> pd.DataFrame:
    """Panel ATR, copied from upstream including the simple rolling mean."""
    previous_close = close.shift(1)
    components = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
        keys=["range", "high_gap", "low_gap"],
    )
    true_range = components.T.groupby(level=1, sort=False).max().T
    return true_range.rolling(window, min_periods=window).mean()


def size_standard_lots(
    *,
    nav: float,
    price: float,
    atr: float,
    multiplier: float,
    lot_size: float,
    risk_fraction: float = 0.005,
    notional_cap: float = 0.20,
) -> int:
    """Size whole FX lots by stop-distance risk and gross notional."""
    values = (nav, price, atr, multiplier, lot_size)
    if not all(np.isfinite(value) and value > 0.0 for value in values):
        return 0
    risk_per_lot = atr * multiplier * lot_size
    notional_per_lot = price * multiplier * lot_size
    by_risk = math.floor(nav * risk_fraction / risk_per_lot)
    by_notional = math.floor(nav * notional_cap / notional_per_lot)
    return max(0, min(by_risk, by_notional))


def size_futures_contracts(
    *,
    nav: float,
    price: float,
    atr: float,
    multiplier: float,
    margin: float,
    risk_fraction: float = 0.005,
    notional_cap: float = 0.25,
    margin_cap: float = 0.15,
) -> int:
    """Size whole contracts using independent risk and exposure ceilings."""
    values = (nav, price, atr, multiplier)
    if not all(np.isfinite(value) and value > 0.0 for value in values):
        return 0
    limits = [
        math.floor(nav * risk_fraction / (atr * multiplier)),
        math.floor(nav * notional_cap / (price * multiplier)),
    ]
    if np.isfinite(margin) and margin > 0.0:
        limits.append(math.floor(nav * margin_cap / margin))
    return max(0, min(limits))
