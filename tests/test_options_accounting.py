"""Deterministic tests for the premium-based options ledger (P2 stage 6, Part 8).

Realized P&L is measured as a premium movement scaled by the traded contract size, and it
is *symmetric*: a short opened at 120 and covered at 100 books the same +1,500 that a long
opened at 100 and sold at 120 does (the equity ``Portfolio`` default cannot, and is pinned
not to).  Costs come from the Stage 5 engine, are assessed once per action, and
``realized_net`` is always ``realized_gross - total_costs``.  Invalid states - over-close,
duplicate exit, side flip, non-positive amounts, back-dated fills - are refused.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest

from nifty_quant.engine.options_accounting import (
    LONG,
    SHORT,
    OptionsAccountingError,
    OptionsLedger,
)
from nifty_quant.engine.options_costs import IndiaOptionsCommission
from nifty_quant.engine.orders import OrderSide

CON = ("NIFTY", "2025-01-30", 22000, "CE")
CON2 = ("NIFTY", "2025-01-30", 22100, "PE")
DAY = "2025-01-02"


def t(minutes: int) -> datetime:
    return datetime(2025, 1, 2, 9, 15) + timedelta(minutes=minutes)


# ---------------------------------------------------------------------------
# Long / short symmetry
# ---------------------------------------------------------------------------
def test_a_long_round_trip_realises_the_premium_movement():
    led = OptionsLedger(100_000.0)
    led.open(CON, LONG, 100.0, 1, 75, DAY, timestamp=t(0))
    led.close(CON, 120.0, 1, DAY, timestamp=t(60))
    assert led.realized_gross == pytest.approx((120.0 - 100.0) * 75 * 1)   # 1500
    assert led.cash == pytest.approx(101_500.0)
    assert led.positions == {}


def test_a_short_round_trip_is_credited_symmetrically():
    led = OptionsLedger(100_000.0)
    led.open(CON, SHORT, 120.0, 1, 75, DAY, timestamp=t(0))
    assert led.cash == pytest.approx(109_000.0)      # premium received to open a short
    led.close(CON, 100.0, 1, DAY, timestamp=t(60))
    assert led.realized_gross == pytest.approx((120.0 - 100.0) * 75 * 1)   # the gain is booked
    assert led.cash == pytest.approx(101_500.0)


# ---------------------------------------------------------------------------
# Partial exits, multiple lots, scale-in
# ---------------------------------------------------------------------------
def test_a_partial_exit_realises_only_the_closed_lots():
    led = OptionsLedger(100_000.0)
    led.open(CON, LONG, 100.0, 2, 50, DAY, timestamp=t(0))
    led.close(CON, 150.0, 1, DAY, timestamp=t(60))
    assert led.realized_gross == pytest.approx((150.0 - 100.0) * 50 * 1)   # 2500
    assert led.positions[CON].lots == 1
    assert led.positions[CON].avg_premium == pytest.approx(100.0)          # untouched
    assert led.open_lots == 1


def test_multiple_lots_scale_the_contract_quantity():
    led = OptionsLedger(100_000.0)
    led.open(CON, LONG, 100.0, 3, 75, DAY, timestamp=t(0))
    led.close(CON, 120.0, 3, DAY, timestamp=t(60))
    assert led.realized_gross == pytest.approx((120.0 - 100.0) * 75 * 3)   # 4500


def test_scaling_in_weights_the_average_entry_premium():
    led = OptionsLedger(100_000.0)
    led.open(CON, LONG, 100.0, 1, 75, DAY, timestamp=t(0))
    led.open(CON, LONG, 120.0, 1, 75, DAY, timestamp=t(30))
    assert led.positions[CON].lots == 2
    assert led.positions[CON].avg_premium == pytest.approx(110.0)


# ---------------------------------------------------------------------------
# Exposure and valuation
# ---------------------------------------------------------------------------
def test_max_capital_exposure_tracks_the_peak_premium_at_risk():
    led = OptionsLedger(100_000.0)
    led.open(CON, LONG, 100.0, 1, 75, DAY, timestamp=t(0))     # outlay 7500
    led.open(CON2, LONG, 200.0, 1, 75, DAY, timestamp=t(10))   # outlay 15000 -> peak 22500
    assert led.max_capital_exposure == pytest.approx(22_500.0)
    led.close(CON, 110.0, 1, DAY, timestamp=t(20))
    assert led.max_capital_exposure == pytest.approx(22_500.0)  # peak never lowers


def test_unrealized_pnl_marks_open_positions_and_skips_unusable_prices():
    led = OptionsLedger(100_000.0)
    led.open(CON, LONG, 100.0, 1, 75, DAY, timestamp=t(0))
    assert led.unrealized_pnl({CON: 110.0}) == pytest.approx((110.0 - 100.0) * 75)
    assert led.unrealized_pnl({}) == 0.0
    assert led.unrealized_pnl({CON: math.nan}) == 0.0          # never guessed


# ---------------------------------------------------------------------------
# Cost handling
# ---------------------------------------------------------------------------
def test_costs_are_assessed_once_and_subtracted_from_realized_net():
    commission = IndiaOptionsCommission()   # default schedule: every rate estimated
    led = OptionsLedger(100_000.0, commission=commission)
    led.open(CON, LONG, 100.0, 1, 75, DAY, timestamp=t(0))
    led.close(CON, 120.0, 1, DAY, timestamp=t(60))

    entry = commission.assess(100.0, 75, 1, OrderSide.BUY, DAY).total
    exit_ = commission.assess(120.0, 75, 1, OrderSide.SELL, DAY).total
    assert led.total_costs == pytest.approx(entry + exit_)     # counted exactly once per leg
    assert led.realized_net == pytest.approx(1500.0 - (entry + exit_))


def test_an_estimated_schedule_marks_the_ledger_not_strict_verifiable():
    led = OptionsLedger(100_000.0, commission=IndiaOptionsCommission())
    led.open(CON, LONG, 100.0, 1, 75, DAY, timestamp=t(0))
    assert led.strict_verifiable is False
    assert led.summary()["strict_verifiable"] is False


def test_no_commission_keeps_the_ledger_strict_verifiable():
    led = OptionsLedger(100_000.0)
    led.open(CON, LONG, 100.0, 1, 75, DAY, timestamp=t(0))
    led.close(CON, 120.0, 1, DAY, timestamp=t(60))
    assert led.strict_verifiable is True
    assert led.total_costs == 0.0


# ---------------------------------------------------------------------------
# Invalid-state guards
# ---------------------------------------------------------------------------
def test_over_closing_more_lots_than_are_open_is_refused():
    led = OptionsLedger(100_000.0)
    led.open(CON, LONG, 100.0, 1, 75, DAY, timestamp=t(0))
    with pytest.raises(OptionsAccountingError):
        led.close(CON, 120.0, 2, DAY, timestamp=t(60))


def test_closing_a_flat_contract_is_refused_as_a_duplicate_exit():
    led = OptionsLedger(100_000.0)
    led.open(CON, LONG, 100.0, 1, 75, DAY, timestamp=t(0))
    led.close(CON, 120.0, 1, DAY, timestamp=t(60))
    with pytest.raises(OptionsAccountingError):
        led.close(CON, 120.0, 1, DAY, timestamp=t(90))


def test_flipping_the_side_of_an_open_contract_is_refused():
    led = OptionsLedger(100_000.0)
    led.open(CON, LONG, 100.0, 1, 75, DAY, timestamp=t(0))
    with pytest.raises(OptionsAccountingError):
        led.open(CON, SHORT, 120.0, 1, 75, DAY, timestamp=t(30))


@pytest.mark.parametrize("bad", [0.0, -5.0, math.nan, math.inf])
def test_a_non_positive_or_non_finite_premium_is_refused(bad):
    led = OptionsLedger(100_000.0)
    with pytest.raises(OptionsAccountingError):
        led.open(CON, LONG, bad, 1, 75, DAY, timestamp=t(0))


@pytest.mark.parametrize("lots,lot_size", [(0, 75), (1, 0), (-1, 75), (1, -50)])
def test_invalid_lot_or_lots_is_refused(lots, lot_size):
    led = OptionsLedger(100_000.0)
    with pytest.raises(OptionsAccountingError):
        led.open(CON, LONG, 100.0, lots, lot_size, DAY, timestamp=t(0))


def test_a_back_dated_fill_is_refused():
    led = OptionsLedger(100_000.0)
    led.open(CON, LONG, 100.0, 1, 75, DAY, timestamp=t(60))
    with pytest.raises(OptionsAccountingError):
        led.close(CON, 120.0, 1, DAY, timestamp=t(30))   # earlier than the entry
