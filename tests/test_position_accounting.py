"""T9: position accounting and equity calculation.

The execution engine's fill/cost behaviour is pinned in ``test_fill_engine.py`` and
``test_costs.py``; this file starts where those leave off - what a sequence of fills does
to cash, positions, average entry, realized P&L and equity, and the cash guard that stops
a buy from becoming silent leverage.

Every number below is written out so the arithmetic can be checked by eye. Realized P&L is
long-side only, which is a documented limitation of this accounting model, so the tests
that show a short closing without credit pin the behaviour as deliberately as the tests
that show the long side.
"""

from __future__ import annotations

import math

import pytest

from nifty_quant.engine.accounting import CASH_GUARD_BUFFER, Portfolio
from nifty_quant.engine.orders import Fill, OrderSide, OrderType

TEST = "TEST"


def fill(side, quantity, price, *, commission=0.0, slippage=0.0,
         order_type=OrderType.MARKET, instrument=TEST, order_id="o1",
         fees=None, timestamp=None, tag=""):
    return Fill(order_id=order_id, instrument=instrument, side=side,
                quantity=quantity, fill_price=price, order_type=order_type,
                slippage=slippage, commission=commission, timestamp=timestamp,
                tag=tag, fees_detail=fees or {})


def buy(quantity, price, **kwargs):
    return fill(OrderSide.BUY, quantity, price, **kwargs)


def sell(quantity, price, **kwargs):
    return fill(OrderSide.SELL, quantity, price, **kwargs)


def portfolio(cash=100_000.0) -> Portfolio:
    return Portfolio(initial_capital=cash)


# ---------------------------------------------------------------------------
# Cash and position bookkeeping
# ---------------------------------------------------------------------------
def test_a_fresh_portfolio_holds_only_its_capital():
    p = portfolio()
    assert p.cash == 100_000.0
    assert p.equity({}) == 100_000.0
    assert p.positions == {} and p.realized_gross == 0.0


def test_the_initial_capital_always_wins_over_a_supplied_cash_field():
    assert Portfolio(initial_capital=500.0, cash=99_999.0).cash == 500.0


def test_a_buy_debits_the_notional_and_the_commission():
    p = portfolio()
    p.apply_fill(buy(10.0, 100.0, commission=20.0))
    assert p.cash == pytest.approx(100_000.0 - 1_000.0 - 20.0)
    assert p.positions[TEST] == 10.0
    assert p.avg_entry[TEST] == 100.0
    assert p.fees_paid == 20.0
    assert p.turnover == 1_000.0


def test_a_sell_credits_the_notional_less_the_commission():
    p = portfolio()
    p.apply_fill(buy(10.0, 100.0, commission=20.0))
    p.apply_fill(sell(10.0, 120.0, commission=20.0))
    assert p.cash == pytest.approx(100_000.0 - 1_000.0 - 20.0 + 1_200.0 - 20.0)
    assert p.positions[TEST] == 0.0
    assert p.fees_paid == 40.0
    assert p.turnover == pytest.approx(1_000.0 + 1_200.0)


def test_slippage_is_tracked_separately_from_the_commission():
    p = portfolio()
    p.apply_fill(buy(10.0, 100.0, commission=20.0, slippage=0.5))
    assert p.slippage_paid == pytest.approx(0.5 * 10.0)
    assert p.fees_paid == 20.0
    # neither of them touches the position or the realized result
    assert p.positions[TEST] == 10.0 and p.realized_gross == 0.0


# ---------------------------------------------------------------------------
# Average entry and realized P&L
# ---------------------------------------------------------------------------
def test_adding_to_a_long_weights_the_average_entry():
    p = portfolio()
    p.apply_fill(buy(10.0, 100.0))
    p.apply_fill(buy(10.0, 120.0))
    assert p.positions[TEST] == 20.0
    # (100*10 + 120*10) / 20
    assert p.avg_entry[TEST] == pytest.approx(110.0)


def test_partial_closing_realises_only_the_part_that_closed():
    p = portfolio()
    p.apply_fill(buy(10.0, 100.0))
    p.apply_fill(sell(4.0, 150.0))
    assert p.realized_gross == pytest.approx((150.0 - 100.0) * 4.0)
    assert p.positions[TEST] == 6.0
    assert p.avg_entry[TEST] == 100.0        # untouched by a partial close
    assert p.fills[-1]["realized_gross"] == pytest.approx(200.0)


def test_the_last_shares_out_clear_the_average_entry():
    p = portfolio()
    p.apply_fill(buy(10.0, 100.0))
    p.apply_fill(sell(6.0, 110.0))
    p.apply_fill(sell(4.0, 90.0))
    assert p.positions[TEST] == 0.0
    assert TEST not in p.avg_entry
    # 6 shares at +10 and 4 at -10
    assert p.realized_gross == pytest.approx(60.0 - 40.0)


def test_a_flip_through_zero_realises_the_whole_long_leg():
    p = portfolio()
    p.apply_fill(buy(10.0, 100.0))
    p.apply_fill(sell(15.0, 120.0))
    assert p.realized_gross == pytest.approx((120.0 - 100.0) * 10.0)
    assert p.positions[TEST] == -5.0
    assert p.avg_entry[TEST] == 100.0        # still the long's entry, no short entry yet


def test_a_buy_that_closes_a_short_replaces_the_entry_without_crediting_the_gain():
    """Documented limitation: realized P&L is measured on the long side only."""
    p = portfolio()
    p.apply_fill(sell(10.0, 100.0))           # short from flat
    assert p.realized_gross == 0.0            # nothing was closed
    assert p.positions[TEST] == -10.0
    p.apply_fill(buy(5.0, 80.0))              # cover half at a lower price
    assert p.positions[TEST] == -5.0
    assert p.realized_gross == 0.0            # the gain is still unrealised
    p.apply_fill(buy(5.0, 80.0))
    assert p.positions[TEST] == 0.0
    # the covering buy re-anchors the entry to its own price rather than blending
    assert p.avg_entry[TEST] == 80.0


def test_repeated_buys_while_short_each_reset_the_entry():
    p = portfolio()
    p.apply_fill(sell(10.0, 100.0))
    p.apply_fill(buy(2.0, 90.0))
    assert p.avg_entry[TEST] == 90.0
    p.apply_fill(buy(2.0, 95.0))
    assert p.avg_entry[TEST] == 95.0          # current (-8) is still <= 0
    assert p.positions[TEST] == -6.0


# ---------------------------------------------------------------------------
# Valuation
# ---------------------------------------------------------------------------
def test_market_value_uses_only_the_prices_it_is_given():
    p = portfolio()
    p.apply_fill(buy(10.0, 100.0, instrument=TEST))
    p.apply_fill(buy(5.0, 200.0, instrument="TCS"))
    assert p.market_value({TEST: 110.0}) == pytest.approx(1_100.0)
    assert p.market_value({TEST: 110.0, "TCS": 210.0}) == pytest.approx(1_100.0 + 1_050.0)


@pytest.mark.parametrize("bad_price", [None, float("nan"), math.inf, -math.inf])
def test_an_unusable_price_is_skipped_rather_than_turned_into_zero(bad_price):
    p = portfolio()
    p.apply_fill(buy(10.0, 100.0))
    prices = {TEST: bad_price} if bad_price is not None else {}
    assert p.market_value(prices) == 0.0
    assert p.equity(prices) == p.cash


def test_a_flat_position_contributes_nothing_even_without_a_price():
    p = portfolio()
    p.apply_fill(buy(10.0, 100.0))
    p.apply_fill(sell(10.0, 100.0))
    assert p.market_value({}) == 0.0
    assert p.equity({}) == p.cash


def test_the_equity_of_a_round_trip_equals_capital_less_costs():
    p = portfolio()
    p.apply_fill(buy(10.0, 100.0, commission=20.0))
    assert p.equity({TEST: 100.0}) == pytest.approx(100_000.0 - 20.0)  # entry at flat price
    p.apply_fill(sell(10.0, 120.0, commission=20.0))
    # gross +200, fees -40
    assert p.equity({TEST: 120.0}) == pytest.approx(100_000.0 + 200.0 - 40.0)


def test_gross_exposure_counts_both_sides_of_the_book():
    p = portfolio()
    p.apply_fill(buy(10.0, 100.0, instrument=TEST))
    p.apply_fill(sell(5.0, 200.0, instrument="TCS"))
    assert p.gross_exposure({TEST: 100.0, "TCS": 200.0}) == pytest.approx(1_000.0 + 1_000.0)
    # an instrument with no price contributes zero, it does not corrupt the total
    assert p.gross_exposure({TEST: 100.0}) == pytest.approx(1_000.0)
    assert p.gross_exposure({TEST: float("nan"), "TCS": 200.0}) == pytest.approx(1_000.0)


# ---------------------------------------------------------------------------
# The cash guard
# ---------------------------------------------------------------------------
def test_the_guard_clips_a_buy_to_what_the_cash_can_actually_fund():
    p = portfolio()
    assert p.affordable_quantity(price=100.0, requested=1_000.0) == 995.0
    # 100_000 / (100 * 1.005) = 995.02 -> 995 whole shares
    assert CASH_GUARD_BUFFER == 1.005


def test_the_buffer_is_a_cost_allowance_not_a_haircut():
    """The 0.5% slack must comfortably absorb the whole statutory cost stack.

    Sized on the configured schedule for a delivery buy: STT 0.1%, brokerage capped at
    20, exchange and stamp charges - so a leg the guard funds can never overdraw cash.
    """
    p = portfolio()
    quantity = p.affordable_quantity(price=100.0, requested=1_000.0)      # 995
    notional = quantity * 100.0                                          # 99_500
    worst_case_commission = (0.001 * notional + 20.0 + 0.00003 * notional
                             + 0.00015 * notional)                        # 137.41
    assert worst_case_commission < 100_000.0 - notional                   # < 500 slack
    p.apply_fill(buy(quantity, 100.0, commission=worst_case_commission))
    assert p.cash == pytest.approx(100_000.0 - notional - worst_case_commission)
    assert p.cash >= 0.0
    assert p.affordable_quantity(price=100.0, requested=1_000.0) == 3.0


def test_the_guard_never_increases_a_smaller_request():
    p = portfolio()
    assert p.affordable_quantity(price=100.0, requested=10.0) == 10.0


@pytest.mark.parametrize("price", [0.0, -100.0, float("nan"), math.inf])
def test_the_guard_refuses_to_size_against_an_unusable_price(price):
    assert portfolio().affordable_quantity(price=price, requested=100.0) == 0.0


def test_the_guard_uses_the_cash_that_is_left_not_the_capital_it_started_with():
    p = portfolio()
    before = p.affordable_quantity(price=100.0, requested=10_000.0)
    p.apply_fill(buy(10.0, 100.0, commission=20.0))
    after = p.affordable_quantity(price=100.0, requested=10_000.0)
    assert before == 995.0
    assert after == 984.0                      # 98_980 / 100.5 = 984.87
    assert after < before


def test_a_buysized_by_the_guard_cannot_overdraw_the_account():
    p = portfolio(cash=10_000.0)
    quantity = p.affordable_quantity(price=250.0, requested=1_000.0)
    assert quantity == 39.0                    # 10_000 / 251.25 = 39.8
    p.apply_fill(buy(quantity, 250.0, commission=20.0))
    assert p.cash == pytest.approx(10_000.0 - 39.0 * 250.0 - 20.0)
    assert p.cash > 0.0


# ---------------------------------------------------------------------------
# The fill ledger
# ---------------------------------------------------------------------------
def test_every_fill_is_written_to_an_auditable_ledger():
    p = portfolio()
    fees = {"brokerage": 20.0, "stt": 2.5, "exchange": 0.03, "gst": 3.6}
    p.apply_fill(buy(10.0, 100.0, commission=20.0, slippage=0.5, fees=fees,
                     order_type=OrderType.LIMIT, order_id="abc", tag="entry",
                     timestamp="2026-01-05 09:20:00"))
    row = p.fills[-1]
    assert row["symbol"] == TEST and row["order_id"] == "abc" and row["tag"] == "entry"
    assert row["side"] == "buy" and row["order_type"] == "limit"
    assert row["timestamp"] == "2026-01-05 09:20:00"
    assert row["quantity"] == 10.0 and row["price"] == 100.0
    assert row["notional"] == 1_000.0
    assert row["commission"] == 20.0 and row["slippage_cost"] == 5.0
    assert row["fees"] == fees
    assert row["realized_gross"] == 0.0
    assert row["cash_after"] == p.cash and row["position_after"] == 10.0


def test_an_explicit_timestamp_wins_over_the_ones_built_into_the_fill():
    p = portfolio()
    p.apply_fill(buy(10.0, 100.0, timestamp="2026-01-05 09:15:00"))
    p.apply_fill(sell(10.0, 100.0), timestamp="2026-01-05 15:30:00")
    assert p.fills[0]["timestamp"] == "2026-01-05 09:15:00"   # from the fill
    assert p.fills[1]["timestamp"] == "2026-01-05 15:30:00"   # from the call site


def test_the_ledger_keeps_the_running_position_across_a_sequence():
    p = portfolio()
    p.apply_fill(buy(10.0, 100.0))
    p.apply_fill(buy(5.0, 110.0))
    p.apply_fill(sell(8.0, 120.0))
    p.apply_fill(sell(7.0, 95.0))
    assert [row["position_after"] for row in p.fills] == [10.0, 15.0, 7.0, 0.0]
    assert p.positions[TEST] == 0.0
    # entry (100*10 + 110*5)/15 = 103.333: 8*(120-103.333) + 7*(95-103.333) = 75
    assert p.realized_gross == pytest.approx(75.0)
