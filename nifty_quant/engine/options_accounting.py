"""Premium-based options position accounting (P2 stage 6, directive Part 8).

Where the equity :class:`~nifty_quant.engine.accounting.Portfolio` measures realized P&L
on the **long side only** (a deliberate, test-pinned limitation of that share-based model),
this ledger is purpose-built for option contracts and is *symmetric*: opening a short and
covering it at a lower premium books the gain, exactly as opening a long and selling it
higher does.  That symmetry is what the directive's "fix long-side-only realised P&L" asks
for; it lives here so the pinned equity behaviour is left untouched.

Every quantity is a *premium* movement scaled by the traded contract size:

    realized_gross = (exit_premium - entry_premium) x lot_size x lots      (a long)

with the equivalent inverted sign for a short.  ``lot_size`` is the historical board lot
resolved by the caller (Stage 2 reference DB), and ``lots`` may be greater than one.

Costs come from the Stage 5 engine and are assessed **once** per action, on the leg's own
effective-dated schedule, so a partial exit prorates correctly and a round trip is never
double-charged.  ``realized_net`` is always ``realized_gross - total_costs``.

Invalid-state guards (the directive's "prevent negative/invalid quantities, duplicate
exits, position mismatches, double-counted charges, future info in earlier decisions"):
a non-positive/non-finite premium, lot or lot size is refused; an exit larger than the
open position is refused; covering a contract that is already flat is refused; opening the
opposite side of a contract that is already open is refused; and a fill stamped earlier
than the last recorded one is refused (no back-dated / future-information entry).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from nifty_quant.engine.options_costs import IndiaOptionsCommission
from nifty_quant.engine.orders import OrderSide

LONG = "long"
SHORT = "short"


def _valid(value: object) -> bool:
    try:
        numeric = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(numeric)


class OptionsAccountingError(ValueError):
    """A ledger action would create an invalid or inconsistent position."""


@dataclass
class _OpenPosition:
    contract: tuple
    side: str
    lots: int
    lot_size: int
    avg_premium: float
    cost_basis: float  # premium outlay for a long; notional received for a short


@dataclass
class OptionsLedger:
    """Cash and realized/unrealized accounting for long and short option positions."""

    initial_capital: float
    commission: IndiaOptionsCommission | None = None
    cash: float = 0.0
    positions: dict[tuple, _OpenPosition] = field(default_factory=dict)
    realized_gross: float = 0.0
    total_costs: float = 0.0
    max_capital_exposure: float = 0.0
    strict_verifiable: bool = True
    entries: list[dict[str, Any]] = field(default_factory=list)
    _last_timestamp: Any = None

    def __post_init__(self) -> None:
        self.cash = float(self.initial_capital)

    # ---- primitives --------------------------------------------------------
    def _stamp_ok(self, timestamp: Any) -> None:
        """Refuse a fill that predates the last recorded one (no future/back-dated info)."""
        if timestamp is None or self._last_timestamp is None:
            self._last_timestamp = timestamp if self._last_timestamp is None else self._last_timestamp
            return
        try:
            if timestamp < self._last_timestamp:
                raise OptionsAccountingError(
                    f"fill timestamp {timestamp!r} precedes last {self._last_timestamp!r}")
        except TypeError:
            pass  # unorderable stamps: skip the monotonicity check rather than crash
        self._last_timestamp = timestamp

    def _cost(self, premium: float, lot_size: int, lots: int,
              side: OrderSide, on_date) -> float:
        if self.commission is None:
            return 0.0
        result = self.commission.assess(premium, lot_size, lots, side, on_date)
        if not result.strict_verifiable:
            self.strict_verifiable = False
        return result.total

    @staticmethod
    def _validate_amounts(premium, lot_size, lots) -> None:
        if not _valid(premium) or float(premium) <= 0.0:
            raise OptionsAccountingError(f"premium must be positive and finite: {premium!r}")
        if int(lot_size) <= 0:
            raise OptionsAccountingError(f"lot_size must be a positive integer: {lot_size!r}")
        if int(lots) <= 0:
            raise OptionsAccountingError(f"lots must be a positive integer: {lots!r}")

    def _exposure(self) -> float:
        """Premium currently at risk across open positions (long cost basis + short notional)."""
        return sum(abs(p.cost_basis) for p in self.positions.values())

    # ---- opening -----------------------------------------------------------
    def open(self, contract: tuple, side: str, premium: float, lots: int,
             lot_size: int, on_date, timestamp: Any = None) -> dict[str, Any]:
        if side not in (LONG, SHORT):
            raise OptionsAccountingError(f"side must be 'long' or 'short': {side!r}")
        self._validate_amounts(premium, lot_size, lots)
        self._stamp_ok(timestamp)

        existing = self.positions.get(contract)
        if existing is not None and existing.side != side:
            raise OptionsAccountingError(
                f"contract {contract} is already open {existing.side}; close before flipping")

        buy_side = OrderSide.BUY if side == LONG else OrderSide.SELL
        costs = self._cost(premium, lot_size, lots, buy_side, on_date)
        self.total_costs += costs
        notional = float(premium) * int(lot_size) * int(lots)

        if existing is None:
            self.positions[contract] = _OpenPosition(
                contract=contract, side=side, lots=int(lots), lot_size=int(lot_size),
                avg_premium=float(premium), cost_basis=notional)
        else:  # scale in on the same side: weight the average entry premium
            total_lots = existing.lots + int(lots)
            existing.avg_premium = (existing.avg_premium * existing.lots + premium * int(lots)) / total_lots
            existing.lots = total_lots
            existing.cost_basis += notional

        if side == LONG:
            self.cash -= notional + costs        # buying a long option pays premium + charges
        else:
            self.cash += notional - costs        # selling to open receives premium less charges

        self.max_capital_exposure = max(self.max_capital_exposure, self._exposure())
        record = {
            "action": "open", "timestamp": timestamp, "contract": contract, "side": side,
            "premium": float(premium), "lots": int(lots), "lot_size": int(lot_size),
            "notional": notional, "costs": costs, "realized": 0.0,
            "cash_after": self.cash, "open_lots": self.positions[contract].lots,
        }
        self.entries.append(record)
        return record

    # ---- closing -----------------------------------------------------------
    def close(self, contract: tuple, premium: float, lots: int, on_date,
              timestamp: Any = None, reason: str | None = None) -> dict[str, Any]:
        self._validate_amounts(premium, 1, lots)
        self._stamp_ok(timestamp)

        position = self.positions.get(contract)
        if position is None:
            raise OptionsAccountingError(f"cannot close {contract}: no open position (duplicate exit?)")
        if int(lots) > position.lots:
            raise OptionsAccountingError(
                f"cannot close {lots} lots, only {position.lots} open (over-close)")

        exit_side = OrderSide.SELL if position.side == LONG else OrderSide.BUY
        costs = self._cost(premium, position.lot_size, lots, exit_side, on_date)
        self.total_costs += costs
        notional = float(premium) * position.lot_size * int(lots)

        if position.side == LONG:
            realized = (float(premium) - position.avg_premium) * position.lot_size * int(lots)
            self.cash += notional - costs        # selling a long receives premium less charges
        else:
            realized = (position.avg_premium - float(premium)) * position.lot_size * int(lots)
            self.cash -= notional + costs        # covering a short pays premium + charges
        self.realized_gross += realized

        remaining = position.lots - int(lots)
        if remaining == 0:
            del self.positions[contract]
        else:
            position.lots = remaining
            position.cost_basis = position.avg_premium * position.lot_size * remaining

        record = {
            "action": "close", "timestamp": timestamp, "contract": contract,
            "reason": reason, "premium": float(premium), "lots": int(lots),
            "lot_size": position.lot_size, "notional": notional, "costs": costs,
            "realized": realized, "cash_after": self.cash,
            "open_lots": remaining,
        }
        self.entries.append(record)
        return record

    # ---- valuation ---------------------------------------------------------
    def unrealized_pnl(self, marks: dict[tuple, float]) -> float:
        total = 0.0
        for contract, position in self.positions.items():
            mark = marks.get(contract)
            if mark is None or not _valid(mark):
                continue  # an unmarked open position adds nothing; never guessed
            move = float(mark) - position.avg_premium
            if position.side == SHORT:
                move = -move
            total += move * position.lot_size * position.lots
        return total

    def market_value(self, marks: dict[tuple, float]) -> float:
        total = 0.0
        for contract, position in self.positions.items():
            mark = marks.get(contract)
            if mark is None or not _valid(mark):
                continue
            value = float(mark) * position.lot_size * position.lots
            total += value if position.side == LONG else -value
        return total

    def equity(self, marks: dict[tuple, float]) -> float:
        return self.cash + self.market_value(marks)

    # ---- results -----------------------------------------------------------
    @property
    def realized_net(self) -> float:
        return self.realized_gross - self.total_costs

    @property
    def open_lots(self) -> int:
        return sum(p.lots for p in self.positions.values())

    def summary(self) -> dict[str, Any]:
        return {
            "cash": self.cash,
            "realized_gross": self.realized_gross,
            "total_costs": self.total_costs,
            "realized_net": self.realized_net,
            "max_capital_exposure": self.max_capital_exposure,
            "open_positions": len(self.positions),
            "strict_verifiable": self.strict_verifiable,
        }
