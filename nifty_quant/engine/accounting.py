"""Portfolio accounting: cash, positions, average entry, NAV.

Cash is never allowed to go negative when the cash guard is enabled: a buy that
cannot be funded is clipped to the affordable quantity instead of being silently
leveraged. That is a deliberate adaptation for running the published strategies
across 50 names, where the published per-name fractions would otherwise sum to
several times NAV.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from nifty_quant.engine.orders import Fill, OrderSide

# Buffer used by the cash guard so that statutory fees cannot overdraw the
# account on the entry leg. 0.5% comfortably covers STT + brokerage + GST on a
# delivery buy at the configured cost schedule.
CASH_GUARD_BUFFER = 1.005


@dataclass
class Portfolio:
    """Single-currency long/short cash account."""

    initial_capital: float
    cash: float = 0.0
    positions: dict[str, float] = field(default_factory=dict)
    avg_entry: dict[str, float] = field(default_factory=dict)
    realized_gross: float = 0.0
    fees_paid: float = 0.0
    slippage_paid: float = 0.0
    turnover: float = 0.0
    fills: list[dict[str, object]] = field(default_factory=list)
    # Default OFF so the share-based equity path is bit-for-bit unchanged (its
    # long-side-only realized behaviour is deliberately pinned by the test suite).
    # Turning it ON credits a buy that covers a short, giving symmetric realized P&L;
    # the premium-based option model lives in ``options_accounting`` instead.
    symmetric_realized: bool = False

    def __post_init__(self) -> None:
        self.cash = float(self.initial_capital)

    # ---- fills ----------------------------------------------------------
    def apply_fill(self, fill: Fill, timestamp: object | None = None) -> None:
        quantity = float(fill.quantity)
        price = float(fill.fill_price)
        notional = price * quantity
        self.fees_paid += float(fill.commission)
        self.slippage_paid += float(fill.slippage_cost)
        self.turnover += abs(notional)

        current = self.positions.get(fill.instrument, 0.0)
        entry = self.avg_entry.get(fill.instrument)
        realized = 0.0

        if fill.side == OrderSide.BUY:
            self.cash -= notional + float(fill.commission)
            new_qty = current + quantity
            if self.symmetric_realized and current < 0 and entry is not None:
                closed_short = min(quantity, -current)
                if closed_short > 0:
                    realized = (entry - price) * closed_short
                    self.realized_gross += realized
                if new_qty > 1e-9:
                    self.avg_entry[fill.instrument] = price   # flip credits short, opens long here
                elif abs(new_qty) <= 1e-9:
                    self.avg_entry.pop(fill.instrument, None)
                # still net short: keep the original short entry to measure against
            elif entry is None or current <= 0:
                self.avg_entry[fill.instrument] = price
            else:
                self.avg_entry[fill.instrument] = (entry * current + price * quantity) / new_qty
            self.positions[fill.instrument] = new_qty
        else:
            self.cash += notional - float(fill.commission)
            closed = min(quantity, max(current, 0.0))
            if entry is not None and closed > 0:
                realized = (price - entry) * closed
                self.realized_gross += realized
            new_qty = current - quantity
            if self.symmetric_realized and new_qty < 0:
                # record the short's own average entry so a later cover can be credited
                opened_short = quantity - closed
                if current < 0 and entry is not None:
                    self.avg_entry[fill.instrument] = (
                        entry * (-current) + price * opened_short) / (-new_qty)
                else:
                    self.avg_entry[fill.instrument] = price
            self.positions[fill.instrument] = new_qty
            if abs(new_qty) < 1e-9:
                self.positions[fill.instrument] = 0.0
                self.avg_entry.pop(fill.instrument, None)

        self.fills.append(
            {
                "timestamp": timestamp if timestamp is not None else fill.timestamp,
                "symbol": fill.instrument,
                "order_id": fill.order_id,
                "side": fill.side.value,
                "quantity": quantity,
                "price": price,
                "notional": notional,
                "order_type": fill.order_type.value,
                "tag": fill.tag,
                "commission": float(fill.commission),
                "slippage_cost": float(fill.slippage_cost),
                "realized_gross": realized,
                "cash_after": self.cash,
                "position_after": self.positions[fill.instrument],
                "fees": dict(fill.fees_detail),
            }
        )

    # ---- valuation ------------------------------------------------------
    def market_value(self, prices: dict[str, float]) -> float:
        value = 0.0
        for symbol, quantity in self.positions.items():
            if quantity == 0:
                continue
            price = prices.get(symbol)
            if price is None or not np.isfinite(price):
                continue
            value += quantity * price
        return value

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + self.market_value(prices)

    def gross_exposure(self, prices: dict[str, float]) -> float:
        return sum(abs(q) * prices.get(s, 0.0) for s, q in self.positions.items() if np.isfinite(prices.get(s, np.nan)))

    # ---- cash guard -----------------------------------------------------
    def affordable_quantity(self, price: float, requested: float) -> float:
        """Maximum whole shares affordable at ``price`` without leverage."""
        if price <= 0 or not np.isfinite(price):
            return 0.0
        budget = max(self.cash, 0.0) / (price * CASH_GUARD_BUFFER)
        return float(min(requested, np.floor(budget)))
