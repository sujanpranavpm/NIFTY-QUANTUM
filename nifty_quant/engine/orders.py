"""Order primitives — a faithful subset of the upstream ``quantjourney-bt`` model.

The upstream types are reproduced field-for-field so that the ported strategy
bodies and the ported fill engine behave identically. Extra fields (``tag``,
``fees_detail``) only add auditability; they never change matching behaviour.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class OrderType(Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"
    STOP_TRAIL = "stop_trail"
    STOP_TRAIL_LIMIT = "stop_trail_limit"
    OCO = "oco"
    BRACKET = "bracket"


class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(Enum):
    PENDING = "pending"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


# Same conservative priority as upstream: stops before limits before markets.
ORDER_PRIORITY: dict[OrderType, int] = {
    OrderType.STOP: 0,
    OrderType.STOP_LIMIT: 1,
    OrderType.STOP_TRAIL: 2,
    OrderType.STOP_TRAIL_LIMIT: 3,
    OrderType.OCO: 4,
    OrderType.LIMIT: 5,
    OrderType.MARKET: 6,
    OrderType.BRACKET: 7,
}


@dataclass(frozen=True, slots=True)
class BarData:
    """One OHLCV bar."""

    timestamp: Any
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True, slots=True)
class BracketSpec:
    """Entry + take-profit + stop-loss specification."""

    take_profit_price: float | None = None
    stop_loss_price: float | None = None
    take_profit_pct: float | None = None
    stop_loss_pct: float | None = None
    take_profit_type: OrderType = OrderType.LIMIT
    stop_loss_type: OrderType = OrderType.STOP
    stop_limit_price: float | None = None
    stop_limit_offset: float | None = None
    trail_amount: float | None = None
    trail_percent: float | None = None


@dataclass
class Order:
    """A single order. Mirrors the upstream field set."""

    instrument: str
    side: OrderSide
    quantity: float
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    stop_price: float | None = None
    limit_offset: float | None = None
    trail_amount: float | None = None
    trail_percent: float | None = None
    _trail_anchor: float | None = None
    _limit_activated: bool = False
    _activated_limit_price: float | None = None
    bracket: BracketSpec | None = None
    oco_pair_id: str | None = None
    order_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: float = 0.0
    avg_fill_price: float | None = None
    created_at: Any = None
    expires_after_bars: int | None = None
    _bars_live: int = 0
    tag: str = ""
    rejection_reason: str | None = None

    @property
    def remaining_qty(self) -> float:
        return self.quantity - self.filled_qty

    @property
    def is_active(self) -> bool:
        return self.status in (OrderStatus.PENDING, OrderStatus.PARTIAL)

    def cancel(self) -> None:
        if self.is_active:
            self.status = OrderStatus.CANCELLED

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"Order({self.order_type.value} {self.side.value} {self.remaining_qty:.1f} "
            f"{self.instrument} [{self.status.value}])"
        )


@dataclass(frozen=True, slots=True)
class Fill:
    """Result of matching one order against one bar."""

    order_id: str
    instrument: str
    side: OrderSide
    quantity: float
    fill_price: float
    order_type: OrderType
    slippage: float = 0.0
    commission: float = 0.0
    timestamp: Any = None
    theoretical_price: float | None = None
    remaining_qty: float = 0.0
    order_status: OrderStatus = OrderStatus.FILLED
    tag: str = ""
    fees_detail: dict[str, float] = field(default_factory=dict)

    @property
    def notional(self) -> float:
        return self.fill_price * self.quantity

    @property
    def slippage_cost(self) -> float:
        return self.slippage * self.quantity
