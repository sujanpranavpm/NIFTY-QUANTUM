"""Bar-based order book — a faithful port of the upstream ``FillEngine``.

Behaviour guaranteed identical to the upstream engine:

* **Next-bar semantics.** ``process_bar`` matches orders submitted on earlier
  bars. An order created after bar *t* closes cannot fill on bar *t*.
* **Open fills.** ``fill_at="open"`` fills market orders at ``bar.open`` and the
  slippage model receives the *previous* bar's HLCV, because the current bar's
  high/low/close are not observable at the open.
* **Priority.** Stops are evaluated before limits, which are evaluated before
  markets. When a protective stop and a take-profit are both reachable inside
  one bar, the stop wins — the conservative choice for a long position.
* **Gap handling.** A stop that gaps through fills at the worse open
  (``min(stop, open)`` for a sell), never at the stop price.
* **Limit-price integrity.** A slippage-adjusted fill is clamped back to
  price-or-better, so a simulated limit fill can never violate its limit.
* **Bracket decomposition.** A bracket becomes an entry plus an OCO pair
  (take-profit, stop-loss) that activates on the first entry fill.
* **Expiry.** ``expires_after_bars`` counts processed bars; bars with a fill do
  not count (an actively-filling order is never silently expired).
"""

from __future__ import annotations

import math
import uuid
from collections import defaultdict
from typing import Callable

from nifty_quant.engine.costs import CommissionModel, SlippageModel, ZeroCommission
from nifty_quant.engine.orders import (
    ORDER_PRIORITY,
    BarData,
    BracketSpec,
    Fill,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)

# Called as capacity_fn(instrument, price, remaining_qty, side) -> max quantity.
CapacityFn = Callable[[str, float, float, OrderSide], float]


class FillEngine:
    """Stateful order book matched against OHLCV bars."""

    def __init__(
        self,
        slippage: SlippageModel | None = None,
        commission: CommissionModel | None = None,
        fill_at: str = "open",
        max_volume_participation: float | None = None,
        capacity_fn: CapacityFn | None = None,
    ) -> None:
        if fill_at not in ("open", "close"):
            raise ValueError("fill_at must be 'open' or 'close'")
        self.slippage = slippage
        self.commission = commission or ZeroCommission()
        self.fill_at = fill_at
        self.max_volume_participation = max_volume_participation
        self.capacity_fn = capacity_fn
        self.reset()

    # ---- lifecycle ------------------------------------------------------
    def reset(self) -> None:
        self._orders: dict[str, list[Order]] = defaultdict(list)
        self._oco_pairs: dict[str, list[str]] = defaultdict(list)
        self._order_history: list[Order] = []
        self._fill_history: list[Fill] = []
        self._last_bar: dict[str, BarData] = {}
        self._volume_history: dict[str, list[float]] = defaultdict(list)
        self._commission_state: dict[str, dict[str, float]] = {}

    # ---- submission -----------------------------------------------------
    def submit(self, order: Order) -> str:
        """Submit an order. Bracket orders are decomposed; returns the entry id."""
        self._validate_order(order)
        if order.order_type == OrderType.BRACKET:
            return self._submit_bracket(order)
        self._orders[order.instrument].append(order)
        self._order_history.append(order)
        return order.order_id

    def _submit_bracket(self, order: Order) -> str:
        bracket = order.bracket
        if bracket is None:
            raise ValueError("bracket order requires a BracketSpec")

        entry = Order(
            instrument=order.instrument,
            side=order.side,
            quantity=order.quantity,
            order_type=OrderType.LIMIT if order.limit_price else OrderType.MARKET,
            limit_price=order.limit_price,
            expires_after_bars=order.expires_after_bars,
            order_id=order.order_id,
            created_at=order.created_at,
            tag=f"bracket_entry:{order.order_id[:8]}",
        )
        exit_side = OrderSide.SELL if order.side == OrderSide.BUY else OrderSide.BUY
        oco_id = f"oco_{uuid.uuid4().hex[:12]}"

        take_profit = Order(
            instrument=order.instrument,
            side=exit_side,
            quantity=order.quantity,
            order_type=bracket.take_profit_type,
            limit_price=bracket.take_profit_price,
            oco_pair_id=oco_id,
            expires_after_bars=order.expires_after_bars,
            tag=f"bracket_tp:{order.order_id[:8]}",
        )
        stop_loss = Order(
            instrument=order.instrument,
            side=exit_side,
            quantity=order.quantity,
            order_type=bracket.stop_loss_type,
            stop_price=bracket.stop_loss_price,
            limit_price=bracket.stop_limit_price,
            limit_offset=bracket.stop_limit_offset,
            trail_amount=bracket.trail_amount,
            trail_percent=bracket.trail_percent,
            oco_pair_id=oco_id,
            expires_after_bars=order.expires_after_bars,
            tag=f"bracket_sl:{order.order_id[:8]}",
        )
        self._oco_pairs[oco_id] = [take_profit.order_id, stop_loss.order_id]
        take_profit._bracket_parent = entry.order_id  # type: ignore[attr-defined]
        stop_loss._bracket_parent = entry.order_id  # type: ignore[attr-defined]

        self._orders[order.instrument].append(entry)
        self._order_history.extend([entry, take_profit, stop_loss])

        entry._bracket_children = [take_profit, stop_loss]  # type: ignore[attr-defined]
        entry._bracket_spec = bracket  # type: ignore[attr-defined]
        entry._bracket_parent_side = order.side  # type: ignore[attr-defined]
        entry._bracket_activated = False  # type: ignore[attr-defined]
        return entry.order_id

    def cancel(self, order_id: str) -> bool:
        for orders in self._orders.values():
            for order in orders:
                if order.order_id == order_id and order.is_active:
                    order.cancel()
                    return True
        return False

    def cancel_all(self, instrument: str | None = None) -> int:
        count = 0
        targets = [instrument] if instrument else list(self._orders)
        for inst in targets:
            for order in self._orders.get(inst, []):
                if order.is_active:
                    order.cancel()
                    count += 1
        return count

    # ---- matching -------------------------------------------------------
    def process_bar(self, instrument: str, bar: BarData) -> list[Fill]:
        """Match all active orders for one instrument against one bar."""
        fills: list[Fill] = []
        previous = self._last_bar.get(instrument)
        orders = self._orders.get(instrument, [])
        if not orders:
            self._observe_bar(instrument, bar)
            return fills

        slippage_bar = self._slippage_reference_bar(bar, previous)
        remaining_capacity: float | None = None
        if self.max_volume_participation is not None:
            volumes = self._volume_history.get(instrument) or []
            if self.fill_at == "open":
                if not volumes:
                    remaining_capacity = 0.0
                else:
                    remaining_capacity = (sum(volumes) / len(volumes)) * self.max_volume_participation
            else:
                remaining_capacity = max(float(bar.volume), 0.0) * self.max_volume_participation

        for order in sorted(orders, key=lambda o: ORDER_PRIORITY.get(o.order_type, 99)):
            if not order.is_active:
                continue
            if self._expire_before_bar(order, bar):
                continue

            capacity = remaining_capacity
            if self.capacity_fn is not None and order.side == OrderSide.BUY:
                price_ref = self._reference_price(order, bar)
                cash_capacity = self.capacity_fn(order.instrument, price_ref, order.remaining_qty, order.side)
                capacity = cash_capacity if capacity is None else min(capacity, cash_capacity)

            fill = self._try_fill(order, bar, max_quantity=capacity, slippage_bar=slippage_bar)
            if fill is None:
                self._expire_after_bar(order, bar, filled_this_bar=False)
                continue

            fills.append(fill)
            self._fill_history.append(fill)
            if remaining_capacity is not None:
                remaining_capacity = max(0.0, remaining_capacity - fill.quantity)

            children = getattr(order, "_bracket_children", None)
            if children is not None:
                for child in children:
                    if child.is_active:
                        child.quantity = order.filled_qty
                if not getattr(order, "_bracket_activated", False):
                    order._bracket_activated = True  # type: ignore[attr-defined]
                    self._prepare_bracket_children(order, fill)
                    for child in children:
                        self._orders[instrument].append(child)

            if order.oco_pair_id:
                self._cancel_oco_pair(order.oco_pair_id, except_id=order.order_id)
            parent = getattr(order, "_bracket_parent", None)
            if parent is not None:
                self.cancel(parent)

            if order.is_active:
                self._expire_after_bar(order, bar, filled_this_bar=True)

        self._orders[instrument] = [o for o in self._orders[instrument] if o.is_active]
        self._observe_bar(instrument, bar)
        return fills

    def _try_fill(
        self,
        order: Order,
        bar: BarData,
        max_quantity: float | None,
        slippage_bar: BarData,
    ) -> Fill | None:
        if not self._bar_supports_order(order, bar):
            return None
        if order.order_type in (OrderType.STOP_TRAIL, OrderType.STOP_TRAIL_LIMIT):
            self._update_trail_anchor(order, bar)

        theoretical = self._theoretical_price(order, bar)
        if theoretical is None or not self._valid(theoretical):
            return None

        quantity = self._fill_quantity(order, max_quantity)
        if quantity <= 0:
            return None

        slipped = (
            self.slippage.compute(price=theoretical, quantity=quantity, side=order.side, bar=slippage_bar)
            if self.slippage is not None
            else theoretical
        )
        if not self._valid(slipped):
            return None
        fill_price = self._apply_limit_constraint(order, slipped, theoretical)
        if not self._valid(fill_price):
            return None

        notional = fill_price * quantity
        fees = self._incremental_fees(order, fill_price, quantity, notional)
        commission = fees.pop("total", 0.0)

        previous_qty = order.filled_qty
        order.filled_qty += quantity
        if order.avg_fill_price is None or previous_qty <= 0:
            order.avg_fill_price = fill_price
        else:
            order.avg_fill_price = (
                order.avg_fill_price * previous_qty + fill_price * quantity
            ) / order.filled_qty
        order.status = OrderStatus.FILLED if order.remaining_qty <= 1e-12 else OrderStatus.PARTIAL

        return Fill(
            order_id=order.order_id,
            instrument=order.instrument,
            side=order.side,
            quantity=quantity,
            fill_price=fill_price,
            order_type=order.order_type,
            slippage=abs(fill_price - theoretical),
            commission=commission,
            timestamp=bar.timestamp,
            theoretical_price=theoretical,
            remaining_qty=max(order.remaining_qty, 0.0),
            order_status=order.status,
            tag=order.tag,
            fees_detail=fees,
        )

    # ---- order-type rules ----------------------------------------------
    def _bar_supports_order(self, order: Order, bar: BarData) -> bool:
        def valid(value: object) -> bool:
            return self._valid(value)

        if order.order_type == OrderType.MARKET:
            return valid(bar.open if self.fill_at == "open" else bar.close)
        if order.order_type == OrderType.LIMIT:
            return valid(bar.low if order.side == OrderSide.BUY else bar.high) and valid(bar.open)
        if order.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
            return valid(bar.high if order.side == OrderSide.BUY else bar.low) and valid(bar.open)
        if order.order_type in (OrderType.STOP_TRAIL, OrderType.STOP_TRAIL_LIMIT):
            return valid(bar.open) and valid(bar.high) and valid(bar.low)
        if order.order_type == OrderType.OCO:
            required = [bar.open]
            if order.stop_price is not None:
                required.append(bar.high if order.side == OrderSide.BUY else bar.low)
            if order.limit_price is not None:
                required.append(bar.low if order.side == OrderSide.BUY else bar.high)
            return all(valid(value) for value in required)
        return False

    def _reference_price(self, order: Order, bar: BarData) -> float:
        return bar.open if self.fill_at == "open" else bar.close

    def _theoretical_price(self, order: Order, bar: BarData) -> float | None:
        kind = order.order_type
        if kind == OrderType.MARKET:
            return self._reference_price(order, bar)

        if kind == OrderType.LIMIT:
            if order.limit_price is None:
                return None
            if order.side == OrderSide.BUY and bar.low <= order.limit_price:
                return min(order.limit_price, bar.open)
            if order.side == OrderSide.SELL and bar.high >= order.limit_price:
                return max(order.limit_price, bar.open)
            return None

        if kind == OrderType.STOP:
            if order.stop_price is None:
                return None
            if order.side == OrderSide.BUY and bar.high >= order.stop_price:
                return max(order.stop_price, bar.open)
            if order.side == OrderSide.SELL and bar.low <= order.stop_price:
                return min(order.stop_price, bar.open)
            return None

        if kind == OrderType.STOP_LIMIT:
            return self._stop_limit_price(order, bar)

        if kind == OrderType.STOP_TRAIL:
            effective = self._trail_stop(order)
            if effective is None:
                return None
            if order.side == OrderSide.SELL and bar.low <= effective:
                return min(effective, bar.open)
            if order.side == OrderSide.BUY and bar.high >= effective:
                return max(effective, bar.open)
            return None

        if kind == OrderType.STOP_TRAIL_LIMIT:
            return self._trailing_stop_limit_price(order, bar)

        if kind == OrderType.OCO:
            if order.stop_price is not None:
                if order.side == OrderSide.BUY and bar.high >= order.stop_price:
                    return max(order.stop_price, bar.open)
                if order.side == OrderSide.SELL and bar.low <= order.stop_price:
                    return min(order.stop_price, bar.open)
            if order.limit_price is not None:
                if order.side == OrderSide.BUY and bar.low <= order.limit_price:
                    return min(order.limit_price, bar.open)
                if order.side == OrderSide.SELL and bar.high >= order.limit_price:
                    return max(order.limit_price, bar.open)
            return None

        return None

    def _stop_limit_price(self, order: Order, bar: BarData) -> float | None:
        if order.stop_price is None or order.limit_price is None:
            return None
        reference = bar.open
        if not order._limit_activated:
            if order.side == OrderSide.BUY and bar.high >= order.stop_price:
                order._limit_activated = True
                order._activated_limit_price = order.limit_price
                reference = max(order.stop_price, bar.open)
            elif order.side == OrderSide.SELL and bar.low <= order.stop_price:
                order._limit_activated = True
                order._activated_limit_price = order.limit_price
                reference = min(order.stop_price, bar.open)
            else:
                return None
        return self._limit_fill_price(order, bar, order._activated_limit_price, reference)

    def _trailing_stop_limit_price(self, order: Order, bar: BarData) -> float | None:
        effective = self._trail_stop(order)
        if effective is None:
            return None
        reference = bar.open
        if not order._limit_activated:
            if order.side == OrderSide.SELL and bar.low <= effective:
                order._limit_activated = True
                order._activated_limit_price = self._trail_limit(order, effective)
                reference = min(effective, bar.open)
            elif order.side == OrderSide.BUY and bar.high >= effective:
                order._limit_activated = True
                order._activated_limit_price = self._trail_limit(order, effective)
                reference = max(effective, bar.open)
            else:
                return None
        return self._limit_fill_price(order, bar, order._activated_limit_price, reference)

    def _limit_fill_price(
        self,
        order: Order,
        bar: BarData,
        limit_price: float | None,
        reference: float,
    ) -> float | None:
        if limit_price is None:
            return None
        if order.side == OrderSide.BUY and bar.low <= limit_price:
            return min(limit_price, reference)
        if order.side == OrderSide.SELL and bar.high >= limit_price:
            return max(limit_price, reference)
        return None

    def _trail_stop(self, order: Order) -> float | None:
        anchor = order._trail_anchor
        if anchor is None:
            return None
        if order.trail_amount is not None:
            distance = order.trail_amount
        elif order.trail_percent is not None:
            distance = anchor * order.trail_percent
        else:
            return None
        return anchor - distance if order.side == OrderSide.SELL else anchor + distance

    def _trail_limit(self, order: Order, effective_stop: float) -> float:
        if order.limit_price is not None:
            return order.limit_price
        offset = float(order.limit_offset or 0.0)
        return effective_stop - offset if order.side == OrderSide.SELL else effective_stop + offset

    def _update_trail_anchor(self, order: Order, bar: BarData) -> None:
        current = order._trail_anchor
        if order.side == OrderSide.SELL:
            if current is None or bar.high > current:
                order._trail_anchor = bar.high
        else:
            if current is None or bar.low < current:
                order._trail_anchor = bar.low

    def _apply_limit_constraint(self, order: Order, fill_price: float, theoretical: float) -> float:
        limit = self._active_limit(order, theoretical)
        if limit is None:
            return fill_price
        return min(fill_price, limit) if order.side == OrderSide.BUY else max(fill_price, limit)

    def _active_limit(self, order: Order, theoretical: float) -> float | None:
        if order.order_type == OrderType.LIMIT:
            return order.limit_price
        if order.order_type in (OrderType.STOP_LIMIT, OrderType.STOP_TRAIL_LIMIT):
            return order._activated_limit_price or order.limit_price
        if order.order_type == OrderType.OCO and order.limit_price is not None:
            if order.side == OrderSide.BUY and theoretical <= order.limit_price:
                return order.limit_price
            if order.side == OrderSide.SELL and theoretical >= order.limit_price:
                return order.limit_price
        return None

    # ---- bracket children ----------------------------------------------
    def _prepare_bracket_children(self, entry: Order, fill: Fill) -> None:
        spec: BracketSpec | None = getattr(entry, "_bracket_spec", None)
        parent_side = getattr(entry, "_bracket_parent_side", entry.side)
        if spec is None:
            return
        entry_price = float(entry.avg_fill_price or fill.fill_price)
        is_long = parent_side == OrderSide.BUY
        for child in getattr(entry, "_bracket_children", []):
            if child.tag.startswith("bracket_tp") and spec.take_profit_pct is not None:
                pct = float(spec.take_profit_pct)
                child.limit_price = entry_price * (1.0 + pct if is_long else 1.0 - pct)
            if child.tag.startswith("bracket_sl"):
                if spec.stop_loss_pct is not None and child.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
                    pct = float(spec.stop_loss_pct)
                    child.stop_price = entry_price * (1.0 - pct if is_long else 1.0 + pct)
                if child.order_type == OrderType.STOP_LIMIT and child.limit_price is None:
                    stop = child.stop_price
                    if stop is not None and spec.stop_limit_offset is not None:
                        offset = float(spec.stop_limit_offset)
                        child.limit_price = stop - offset if child.side == OrderSide.SELL else stop + offset

    def _cancel_oco_pair(self, oco_id: str, except_id: str) -> None:
        for order_id in self._oco_pairs.get(oco_id, []):
            if order_id != except_id:
                self.cancel(order_id)

    # ---- plumbing -------------------------------------------------------
    def _fill_quantity(self, order: Order, max_quantity: float | None) -> float:
        remaining = max(float(order.remaining_qty), 0.0)
        if max_quantity is None:
            return remaining
        return min(remaining, max(float(max_quantity), 0.0))

    def _incremental_fees(
        self,
        order: Order,
        price: float,
        quantity: float,
        notional: float,
    ) -> dict[str, float]:
        state = self._commission_state.get(order.order_id, {"quantity": 0.0, "notional": 0.0, "charged": 0.0})
        cumulative_qty = state["quantity"] + abs(quantity)
        cumulative_notional = state["notional"] + abs(notional)
        due = self.commission.compute(
            price=price,
            quantity=cumulative_qty,
            notional=cumulative_notional,
            side=order.side,
        )
        incremental = max(0.0, float(due) - state["charged"])
        breakdown: dict[str, float] = {}
        calculator = getattr(self.commission, "breakdown", None)
        if calculator is not None:
            parts = calculator(price, quantity, notional, order.side)
            breakdown = {k: float(v) for k, v in parts.items() if k != "total"}
        breakdown["total"] = incremental

        self._commission_state[order.order_id] = {
            "quantity": cumulative_qty,
            "notional": cumulative_notional,
            "charged": state["charged"] + incremental,
        }
        return breakdown

    def _slippage_reference_bar(self, bar: BarData, previous: BarData | None) -> BarData:
        if self.fill_at != "open":
            return bar
        if previous is None:
            return BarData(bar.timestamp, bar.open, math.nan, math.nan, math.nan, math.nan)
        return BarData(bar.timestamp, bar.open, previous.high, previous.low, previous.close, previous.volume)

    def _observe_bar(self, instrument: str, bar: BarData) -> None:
        self._last_bar[instrument] = bar
        volume = float(bar.volume)
        if math.isfinite(volume) and volume >= 0.0:
            history = self._volume_history[instrument]
            history.append(volume)
            if len(history) > 20:
                history.pop(0)

    def _expire_before_bar(self, order: Order, bar: BarData) -> bool:
        if order.created_at is None:
            order.created_at = bar.timestamp
        return False

    def _expire_after_bar(self, order: Order, bar: BarData, filled_this_bar: bool) -> bool:
        if not order.is_active:
            return False
        if not filled_this_bar:
            order._bars_live += 1
        if order.expires_after_bars is not None and order._bars_live >= order.expires_after_bars:
            order.status = OrderStatus.EXPIRED
            return True
        return False

    @staticmethod
    def _valid(value: object) -> bool:
        try:
            numeric = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError, OverflowError):
            return False
        return math.isfinite(numeric) and numeric > 0.0

    def _validate_order(self, order: Order) -> None:
        if not math.isfinite(float(order.quantity)) or order.quantity <= 0:
            raise ValueError(f"order quantity must be positive and finite: {order.quantity!r}")
        needs_limit = order.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT)
        if needs_limit and order.limit_price is None:
            raise ValueError(f"{order.order_type.value} order requires limit_price")
        if order.order_type in (OrderType.STOP, OrderType.STOP_LIMIT) and order.stop_price is None:
            raise ValueError(f"{order.order_type.value} order requires stop_price")
        if order.order_type == OrderType.STOP_TRAIL and order.trail_amount is None and order.trail_percent is None:
            raise ValueError("stop_trail order requires trail_amount or trail_percent")

    # ---- introspection --------------------------------------------------
    @property
    def pending_orders(self) -> list[Order]:
        return [o for orders in self._orders.values() for o in orders if o.is_active]

    @property
    def order_history(self) -> list[Order]:
        return list(self._order_history)

    @property
    def fill_history(self) -> list[Fill]:
        return list(self._fill_history)
