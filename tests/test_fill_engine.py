"""T9 - order fills: matching rules, slippage, stop/target execution, priority, expiry.

Each test states the bar data literally and the expected fill price literally, derived
from the documented rules:

* next-bar semantics (``process_bar`` matches orders that existed before the call),
* ``fill_at="open"`` fills at that bar's open and slippage sees the *previous* bar,
* stop-before-limit-before-market priority inside one bar,
* a gapped stop fills at the worse open, never at the stop price,
* a slippage-adjusted limit fill is clamped back to price-or-better,
* a bracket is an entry plus an OCO pair; the first child to fill cancels the other.
"""

from __future__ import annotations

import math

import pytest

from nifty_quant.engine.costs import FixedBpsSlippage, IndiaEquityCommission, ZeroCommission
from nifty_quant.engine.fill_engine import FillEngine
from nifty_quant.engine.orders import (
    BarData,
    BracketSpec,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)
from nifty_quant.config import IndiaEquityCosts


def B(ts, o, h, l, c, v=1000.0) -> BarData:
    return BarData(timestamp=ts, open=o, high=h, low=l, close=c, volume=v)


# --------------------------------------------------------------------------
# Market orders and next-bar semantics
# --------------------------------------------------------------------------
def test_market_order_fills_at_the_open_of_the_bar_it_is_matched_against():
    engine = FillEngine(fill_at="open")
    engine.submit(Order(instrument="TEST", side=OrderSide.BUY, quantity=10))
    fills = engine.process_bar("TEST", B("t1", 100, 102, 99, 101))
    assert len(fills) == 1
    assert fills[0].fill_price == 100.0
    assert fills[0].timestamp == "t1"
    assert fills[0].order_status == OrderStatus.FILLED
    assert fills[0].remaining_qty == 0.0
    assert engine.pending_orders == []


def test_market_order_fills_at_the_close_when_configured_that_way():
    engine = FillEngine(fill_at="close")
    engine.submit(Order(instrument="TEST", side=OrderSide.BUY, quantity=10))
    fills = engine.process_bar("TEST", B("t1", 100, 102, 99, 101))
    assert fills[0].fill_price == 101.0


def test_order_submitted_after_a_bar_is_not_filled_by_that_bar():
    """The single most important anti-look-ahead property of the engine."""
    engine = FillEngine(fill_at="open")
    first = engine.process_bar("TEST", B("t1", 100, 102, 99, 101))
    assert first == []
    engine.submit(Order(instrument="TEST", side=OrderSide.BUY, quantity=10))
    second = engine.process_bar("TEST", B("t2", 110, 112, 108, 111))
    assert len(second) == 1
    assert second[0].fill_price == 110.0          # t2's open, never t1's close
    assert second[0].slippage == 0.0


def test_earlier_fills_are_identical_when_only_a_future_bar_changes():
    shared = [B("t1", 100, 102, 99, 101), B("t2", 101, 103, 100, 102)]
    runs = []
    for tail in ([B("t3", 102, 104, 101, 103)], [B("t3", 500, 600, 1, 550)]):
        engine = FillEngine(fill_at="open")
        engine.submit(Order(instrument="TEST", side=OrderSide.BUY, quantity=10))
        out = [engine.process_bar("TEST", b) for b in shared + tail]
        # Order ids are random uuids, so compare the economics, not the identity.
        runs.append([[(f.fill_price, f.quantity, f.timestamp) for f in fills] for fills in out])
    assert runs[0][:2] == runs[1][:2]
    assert runs[0][:2] == [[(100.0, 10.0, "t1")], []]


def test_fixed_bps_slippage_is_applied_and_reported_per_fill():
    engine = FillEngine(slippage=FixedBpsSlippage(bps=100.0), fill_at="open")   # 1%
    engine.submit(Order(instrument="TEST", side=OrderSide.BUY, quantity=10))
    fills = engine.process_bar("TEST", B("t1", 100, 101, 99, 100))
    assert fills[0].theoretical_price == 100.0
    assert fills[0].fill_price == pytest.approx(101.0)
    assert fills[0].slippage == pytest.approx(1.0)
    assert fills[0].slippage_cost == pytest.approx(10.0)   # per-share x quantity


def test_unfillable_price_data_is_rejected_rather_than_filled():
    engine = FillEngine(fill_at="open")
    for bad_open in (0.0, float("nan"), float("inf")):
        engine.submit(Order(instrument="TEST", side=OrderSide.BUY, quantity=10))
        assert engine.process_bar("TEST", B("t1", bad_open, 102, 99, 101)) == []
        engine.cancel_all()


# --------------------------------------------------------------------------
# Limit orders
# --------------------------------------------------------------------------
def test_limit_buy_fills_at_the_open_when_the_open_is_already_better():
    engine = FillEngine(fill_at="open")
    engine.submit(Order(instrument="TEST", side=OrderSide.BUY, quantity=10,
                        order_type=OrderType.LIMIT, limit_price=105.0))
    fills = engine.process_bar("TEST", B("t1", 100, 102, 99, 101))
    assert fills[0].fill_price == 100.0            # min(limit, open)
    assert fills[0].theoretical_price == 100.0


def test_limit_buy_fills_at_the_limit_when_the_market_trades_through_it():
    engine = FillEngine(fill_at="open")
    engine.submit(Order(instrument="TEST", side=OrderSide.BUY, quantity=10,
                        order_type=OrderType.LIMIT, limit_price=100.0))
    fills = engine.process_bar("TEST", B("t1", 105, 106, 99, 104))
    assert fills[0].fill_price == 100.0            # min(limit, open) = limit


def test_limit_buy_never_fills_when_the_bar_never_reaches_the_limit():
    engine = FillEngine(fill_at="open")
    engine.submit(Order(instrument="TEST", side=OrderSide.BUY, quantity=10,
                        order_type=OrderType.LIMIT, limit_price=98.0))
    assert engine.process_bar("TEST", B("t1", 105, 106, 99, 104)) == []
    assert engine.pending_orders[0].status == OrderStatus.PENDING


def test_limit_sell_requires_the_high_to_reach_the_limit():
    engine = FillEngine(fill_at="open")
    engine.submit(Order(instrument="TEST", side=OrderSide.SELL, quantity=10,
                        order_type=OrderType.LIMIT, limit_price=110.0))
    assert engine.process_bar("TEST", B("t1", 105, 109, 100, 108)) == []
    fills = engine.process_bar("TEST", B("t2", 105, 111, 100, 108))
    assert fills[0].fill_price == 110.0            # max(limit, open) = limit


# --------------------------------------------------------------------------
# Stop orders and gap rules
# --------------------------------------------------------------------------
def test_stop_buy_ignites_only_when_the_high_touches_the_stop():
    engine = FillEngine(fill_at="open")
    engine.submit(Order(instrument="TEST", side=OrderSide.BUY, quantity=10,
                        order_type=OrderType.STOP, stop_price=105.0))
    assert engine.process_bar("TEST", B("t1", 100, 104.9, 99, 103)) == []
    fills = engine.process_bar("TEST", B("t2", 100, 106, 99, 105))
    assert fills[0].fill_price == 105.0            # max(stop, open) = the stop itself


def test_stop_sell_that_gaps_down_fills_at_the_worse_open_not_the_stop():
    engine = FillEngine(fill_at="open")
    engine.submit(Order(instrument="TEST", side=OrderSide.SELL, quantity=10,
                        order_type=OrderType.STOP, stop_price=95.0))
    fills = engine.process_bar("TEST", B("t1", 90.0, 92.0, 88.0, 91.0))
    assert fills[0].fill_price == 90.0             # min(stop, open): never better than reality
    assert fills[0].theoretical_price == 90.0


def test_stop_limit_arms_at_the_stop_and_then_fills_at_the_trigger_price():
    engine = FillEngine(fill_at="open")
    engine.submit(Order(instrument="TEST", side=OrderSide.SELL, quantity=10,
                        order_type=OrderType.STOP_LIMIT, stop_price=95.0, limit_price=93.0))
    assert engine.process_bar("TEST", B("t1", 100, 101, 96, 100)) == []   # not armed
    order = engine.pending_orders[0]
    assert order._limit_activated is False
    fills = engine.process_bar("TEST", B("t2", 96, 96.5, 94.0, 94.5))
    # armed at the stop: reference = min(stop, open) = 95, which is price-or-better
    # against the 93 limit, so the fill is at 95.
    assert fills[0].fill_price == pytest.approx(95.0)
    assert order._limit_activated is True


def test_stop_limit_that_gaps_through_its_stop_but_not_its_limit_does_not_fill():
    engine = FillEngine(fill_at="open")
    engine.submit(Order(instrument="TEST", side=OrderSide.SELL, quantity=10,
                        order_type=OrderType.STOP_LIMIT, stop_price=95.0, limit_price=93.0))
    assert engine.process_bar("TEST", B("t1", 100, 101, 96, 100)) == []
    # Gap down to 92: the stop is through, the trigger reference is 92, and the 93 limit
    # is never reachable at or above it, so a real limit order would stay unfilled.
    assert engine.process_bar("TEST", B("t2", 92, 92.5, 90, 91)) == []
    assert engine.pending_orders[0]._limit_activated is True


# --------------------------------------------------------------------------
# Priority: stop before limit inside one bar
# --------------------------------------------------------------------------
def test_stop_is_evaluated_before_a_limit_in_the_same_bar():
    engine = FillEngine(fill_at="open")
    engine.submit(Order(instrument="TEST", side=OrderSide.SELL, quantity=10,
                        order_type=OrderType.LIMIT, limit_price=101.0, tag="tp"))
    engine.submit(Order(instrument="TEST", side=OrderSide.SELL, quantity=10,
                        order_type=OrderType.STOP, stop_price=99.0, tag="sl"))
    fills = engine.process_bar("TEST", B("t1", 100.0, 102.0, 98.0, 100.0))
    # Two independent orders, both reachable: the stop is priced first (conservative).
    assert fills[0].tag == "sl"
    assert fills[0].fill_price == 99.0
    assert [f.tag for f in fills] == ["sl", "tp"]


# --------------------------------------------------------------------------
# Slippage inside the engine
# --------------------------------------------------------------------------
def test_open_fill_uses_the_previous_bar_for_slippage():
    """A price not observable at the open must not be used to price the fill."""
    from nifty_quant.engine.costs import VolatilitySlippage
    engine = FillEngine(slippage=VolatilitySlippage(vol_factor=0.5), fill_at="open")
    engine.submit(Order(instrument="TEST", side=OrderSide.BUY, quantity=10))
    first = engine.process_bar("TEST", B("t1", 100, 100.4, 99.6, 100))
    assert first[0].fill_price == pytest.approx(100.0)   # no previous bar -> no range known

    engine.submit(Order(instrument="TEST", side=OrderSide.BUY, quantity=10))
    second = engine.process_bar("TEST", B("t2", 100, 140, 60, 100))
    # Slippage must be priced off t1's range (0.8%), not off t2's own 80% range, because
    # at t2's open the high/low of t2 are not yet observable.
    assert second[0].fill_price == pytest.approx(100.4)
    assert second[0].fill_price < 101.0


def test_volatility_slippage_on_the_first_bar_of_a_series_is_not_guessed():
    from nifty_quant.engine.costs import VolatilitySlippage
    engine = FillEngine(slippage=VolatilitySlippage(vol_factor=0.5), fill_at="open")
    engine.submit(Order(instrument="TEST", side=OrderSide.BUY, quantity=10))
    fills = engine.process_bar("TEST", B("t1", 100, 140, 60, 100))
    assert fills[0].fill_price == 100.0            # no previous bar -> no range -> no slippage


def test_slippage_never_breaks_a_limit_price():
    engine = FillEngine(slippage=FixedBpsSlippage(bps=100.0), fill_at="open")
    engine.submit(Order(instrument="TEST", side=OrderSide.BUY, quantity=10,
                        order_type=OrderType.LIMIT, limit_price=100.0))
    fills = engine.process_bar("TEST", B("t1", 99.5, 100.0, 99.0, 99.5))
    # theoretical 99.5, slipped 100.495, clamped back to the limit
    assert fills[0].theoretical_price == pytest.approx(99.5)
    assert fills[0].fill_price == pytest.approx(100.0)
    assert fills[0].fill_price <= 100.0


def test_slippage_never_breaks_a_sell_limit_either():
    engine = FillEngine(slippage=FixedBpsSlippage(bps=100.0), fill_at="open")
    engine.submit(Order(instrument="TEST", side=OrderSide.SELL, quantity=10,
                        order_type=OrderType.LIMIT, limit_price=100.0))
    fills = engine.process_bar("TEST", B("t1", 100.5, 101.0, 100.0, 100.5))
    assert fills[0].fill_price == pytest.approx(100.0)     # max(99.505, limit)
    assert fills[0].fill_price >= 100.0


# --------------------------------------------------------------------------
# Brackets: stop-loss and take-profit execution
# --------------------------------------------------------------------------
def _bracket_engine(**spec) -> tuple[FillEngine, Order]:
    """Engine that has already seen the signal bar, with a bracket submitted after it."""
    engine = FillEngine(fill_at="open")
    engine.process_bar("TEST", B("t0", 99, 100, 98, 99))     # the bar the signal uses
    parent = Order(instrument="TEST", side=OrderSide.BUY, quantity=10,
                   order_type=OrderType.BRACKET, created_at="t0", bracket=BracketSpec(**spec))
    engine.submit(parent)
    return engine, parent


def test_bracket_decomposes_into_entry_and_an_oco_pair_that_activates_on_the_entry_fill():
    engine, _ = _bracket_engine(take_profit_price=110.0, stop_loss_price=95.0)
    # The signal bar (t0) is already gone: the entry can only fill on a later bar.
    entry_fills = engine.process_bar("TEST", B("t1", 100, 102, 99, 101))
    assert len(entry_fills) == 1
    assert entry_fills[0].fill_price == 100.0
    assert entry_fills[0].tag.startswith("bracket_entry")
    tags = {o.tag.split(":")[0] for o in engine.pending_orders}
    assert tags == {"bracket_tp", "bracket_sl"}


def test_bracket_stop_fills_first_and_cancels_the_take_profit():
    engine, _ = _bracket_engine(take_profit_price=110.0, stop_loss_price=95.0)
    engine.process_bar("TEST", B("t1", 100, 102, 99, 101))          # entry at 100
    fills = engine.process_bar("TEST", B("t2", 101, 112, 94, 100))  # both reachable
    assert len(fills) == 1
    assert fills[0].tag.startswith("bracket_sl")
    assert fills[0].fill_price == 95.0
    assert engine.pending_orders == []
    statuses = {o.tag.split(":")[0]: o.status for o in engine.order_history}
    assert statuses["bracket_tp"] == OrderStatus.CANCELLED


def test_bracket_take_profit_fills_when_only_the_target_is_reachable():
    engine, _ = _bracket_engine(take_profit_price=110.0, stop_loss_price=95.0)
    engine.process_bar("TEST", B("t1", 100, 102, 99, 101))
    fills = engine.process_bar("TEST", B("t2", 105, 111, 100, 110))
    assert len(fills) == 1
    assert fills[0].tag.startswith("bracket_tp")
    assert fills[0].fill_price == 110.0
    assert {o.tag.split(":")[0]: o.status for o in engine.order_history}["bracket_sl"] == OrderStatus.CANCELLED


def test_bracket_percent_spec_is_resolved_against_the_actual_entry_fill():
    engine, _ = _bracket_engine(take_profit_pct=0.10, stop_loss_pct=0.05)
    entry = engine.process_bar("TEST", B("t1", 120, 121, 119, 120))     # entry at 120
    assert entry[0].fill_price == 120.0
    children = {o.tag.split(":")[0]: o for o in engine.pending_orders}
    assert children["bracket_tp"].limit_price == pytest.approx(132.0)   # 120 x 1.10
    assert children["bracket_sl"].stop_price == pytest.approx(114.0)    # 120 x 0.95


def test_partial_entry_fill_shrinks_the_bracket_children():
    engine = FillEngine(fill_at="open", max_volume_participation=0.005)
    engine.process_bar("TEST", B("t0", 100, 101, 99, 100, 1000))    # signal bar + volume
    engine.submit(Order(instrument="TEST", side=OrderSide.BUY, quantity=10,
                        order_type=OrderType.BRACKET,
                        bracket=BracketSpec(take_profit_price=110.0, stop_loss_price=95.0)))
    fills = engine.process_bar("TEST", B("t1", 100, 102, 99, 101, 1000))
    assert fills[0].quantity == pytest.approx(5.0)      # 0.5% of the 1000 mean volume
    assert fills[0].order_status == OrderStatus.PARTIAL
    children = [o for o in engine.pending_orders if o.oco_pair_id]
    assert children and all(o.quantity == pytest.approx(5.0) for o in children)


# --------------------------------------------------------------------------
# Trailing stops
# --------------------------------------------------------------------------
def test_trailing_stop_ratchets_up_from_the_observed_high():
    engine = FillEngine(fill_at="open")
    engine.submit(Order(instrument="TEST", side=OrderSide.SELL, quantity=10,
                        order_type=OrderType.STOP_TRAIL, trail_amount=5.0))
    assert engine.process_bar("TEST", B("t1", 100, 100, 99, 100)) == []
    order = engine.pending_orders[0]
    assert order._trail_anchor == 100.0
    engine.process_bar("TEST", B("t2", 104, 106, 103, 105))         # anchor ratchets to 106
    assert order._trail_anchor == 106.0
    assert order._trail_anchor >= 100.0                             # never loosens
    fills = engine.process_bar("TEST", B("t3", 102, 102, 100, 101)) # stop = 101
    assert fills[0].fill_price == pytest.approx(101.0)              # min(stop, open)


def test_trailing_percent_stop_uses_the_anchor_distance():
    engine = FillEngine(fill_at="open")
    engine.submit(Order(instrument="TEST", side=OrderSide.SELL, quantity=10,
                        order_type=OrderType.STOP_TRAIL, trail_percent=0.02))
    engine.process_bar("TEST", B("t1", 100, 100, 99, 100))
    fills = engine.process_bar("TEST", B("t2", 99, 99, 97, 98))     # stop = 98
    assert fills[0].fill_price == pytest.approx(98.0)


# --------------------------------------------------------------------------
# Quantity, participation and expiry
# --------------------------------------------------------------------------
def test_participation_cap_produces_a_partial_then_the_remainder():
    engine = FillEngine(fill_at="open", max_volume_participation=0.1)
    engine.process_bar("TEST", B("t0", 100, 101, 99, 100, 100))     # mean volume 100
    engine.submit(Order(instrument="TEST", side=OrderSide.BUY, quantity=50))
    fills = engine.process_bar("TEST", B("t1", 100, 101, 99, 100, 100))
    assert fills[0].quantity == pytest.approx(10.0)                 # 10% of 100
    assert fills[0].order_status == OrderStatus.PARTIAL
    assert fills[0].remaining_qty == pytest.approx(40.0)


def test_orders_expire_after_the_configured_number_of_non_filling_bars():
    engine = FillEngine(fill_at="open")
    engine.submit(Order(instrument="TEST", side=OrderSide.BUY, quantity=10,
                        order_type=OrderType.LIMIT, limit_price=1.0, expires_after_bars=2))
    assert engine.process_bar("TEST", B("t1", 100, 101, 99, 100)) == []
    assert engine.pending_orders[0].status == OrderStatus.PENDING
    assert engine.process_bar("TEST", B("t2", 100, 101, 99, 100)) == []
    assert engine.pending_orders == []
    assert engine.order_history[0].status == OrderStatus.EXPIRED


def test_a_filling_bar_does_not_count_towards_expiry():
    engine = FillEngine(fill_at="open", max_volume_participation=0.1)
    engine.process_bar("TEST", B("t0", 100, 100, 100, 100, 100))
    engine.submit(Order(instrument="TEST", side=OrderSide.BUY, quantity=20,
                        expires_after_bars=2))
    part = engine.process_bar("TEST", B("t1", 100, 100, 100, 100, 100))
    assert part[0].order_status == OrderStatus.PARTIAL
    rest = engine.process_bar("TEST", B("t2", 101, 101, 101, 101, 100))
    assert rest[0].order_status == OrderStatus.FILLED


# --------------------------------------------------------------------------
# Validation and cancellation
# --------------------------------------------------------------------------
@pytest.mark.parametrize("quantity", [0.0, -5.0, float("nan"), float("inf")])
def test_non_positive_or_non_finite_quantity_is_refused(quantity):
    engine = FillEngine()
    with pytest.raises(ValueError, match="quantity must be positive"):
        engine.submit(Order(instrument="TEST", side=OrderSide.BUY, quantity=quantity))


def test_orders_missing_their_required_price_are_refused():
    engine = FillEngine()
    with pytest.raises(ValueError, match="limit_price"):
        engine.submit(Order(instrument="TEST", side=OrderSide.BUY, quantity=10,
                            order_type=OrderType.LIMIT))
    with pytest.raises(ValueError, match="stop_price"):
        engine.submit(Order(instrument="TEST", side=OrderSide.BUY, quantity=10,
                            order_type=OrderType.STOP))
    with pytest.raises(ValueError, match="trail_amount or trail_percent"):
        engine.submit(Order(instrument="TEST", side=OrderSide.SELL, quantity=10,
                            order_type=OrderType.STOP_TRAIL))
    with pytest.raises(ValueError, match="bracket"):
        engine.submit(Order(instrument="TEST", side=OrderSide.BUY, quantity=10,
                            order_type=OrderType.BRACKET))


def test_fill_at_must_be_open_or_close():
    with pytest.raises(ValueError, match="fill_at"):
        FillEngine(fill_at="mid")


def test_cancel_removes_an_active_order_and_reports_whether_it_existed():
    engine = FillEngine()
    order = Order(instrument="TEST", side=OrderSide.BUY, quantity=10,
                  order_type=OrderType.LIMIT, limit_price=1.0)
    engine.submit(order)
    assert engine.cancel("no-such-id") is False
    assert engine.cancel(order.order_id) is True
    assert order.status == OrderStatus.CANCELLED
    assert engine.cancel(order.order_id) is False      # idempotent
    assert engine.process_bar("TEST", B("t1", 100, 101, 99, 100)) == []


def test_cancel_all_counts_only_active_orders():
    engine = FillEngine()
    for _ in range(3):
        engine.submit(Order(instrument="TEST", side=OrderSide.BUY, quantity=10,
                           order_type=OrderType.LIMIT, limit_price=1.0))
    assert engine.cancel_all("TEST") == 3
    assert engine.cancel_all("OTHER") == 0


# --------------------------------------------------------------------------
# Fees reported by the engine
# --------------------------------------------------------------------------
def test_fill_carries_the_itemised_statutory_components():
    engine = FillEngine(commission=IndiaEquityCommission(IndiaEquityCosts()), fill_at="open")
    engine.submit(Order(instrument="TEST", side=OrderSide.BUY, quantity=10))
    fills = engine.process_bar("TEST", B("t1", 100, 101, 99, 100))
    fees = fills[0].fees_detail
    assert set(fees) >= {"brokerage", "stt", "exchange", "sebi", "stamp_duty", "gst", "dp_charge"}
    # turnover is 10 shares x 100 = 1,000 rupees
    assert fees["brokerage"] == pytest.approx(0.3)        # 0.03%, under the 20 rupee cap
    assert fees["stt"] == pytest.approx(1.0)              # 0.1%
    assert fees["exchange"] == pytest.approx(0.0297)
    assert fees["sebi"] == pytest.approx(0.001)
    assert fees["stamp_duty"] == pytest.approx(0.15)      # 0.015% buy leg
    assert fees["dp_charge"] == 0.0                       # buy leg
    assert fills[0].commission == pytest.approx(1.540226)   # sum of the components above
    assert fills[0].commission == pytest.approx(sum(fees.values()))
    assert "total" not in fees             # the total is surfaced as Fill.commission


def test_zero_commission_is_the_default_and_costs_nothing():
    engine = FillEngine()
    assert isinstance(engine.commission, ZeroCommission)
    engine.submit(Order(instrument="TEST", side=OrderSide.BUY, quantity=10))
    fills = engine.process_bar("TEST", B("t1", 100, 101, 99, 100))
    assert fills[0].commission == 0.0
    assert fills[0].fees_detail == {}                # nothing to itemise


def test_average_fill_price_weighted_over_two_partial_fills():
    engine = FillEngine(fill_at="open", max_volume_participation=0.1)
    engine.process_bar("TEST", B("t0", 100, 100, 100, 100, 100))
    order = Order(instrument="TEST", side=OrderSide.BUY, quantity=20)
    engine.submit(order)
    engine.process_bar("TEST", B("t1", 100, 100, 100, 100, 100))   # 10 @ 100
    engine.process_bar("TEST", B("t2", 110, 110, 110, 110, 100))   # 10 @ 110
    assert order.avg_fill_price == pytest.approx(105.0)
    assert order.filled_qty == pytest.approx(20.0)
    assert math.isclose(sum(f.quantity for f in engine.fill_history), 20.0)
