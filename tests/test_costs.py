"""T9 - transaction costs: slippage models, brokerage and statutory charges.

Every number is derived from the published schedule by hand and written literally in
the test, so a change in a constant cannot silently pass.  The itemised NSE model is
the one the rupee exports depend on, so the component identities are pinned too:

* brokerage is the *lower* of percentage-of-turnover and the per-order cap,
* STT is on both legs for delivery and on the sell leg only for intraday,
* stamp duty is on the buy leg only, and at a different rate for intraday,
* GST applies to brokerage + exchange + SEBI only (never to STT, stamp or DP),
* the DP charge is a flat amount on the sell leg of a delivery order only.
"""

from __future__ import annotations

import math

import pytest

from nifty_quant.config import IndiaEquityCosts, SlippageConfig
from nifty_quant.engine.costs import (
    FixedBpsCommission,
    FixedBpsSlippage,
    IndiaEquityCommission,
    NoSlippage,
    PerShareCommission,
    VolatilitySlippage,
    ZeroCommission,
    build_commission,
    build_slippage,
)
from nifty_quant.engine.orders import BarData, OrderSide

T100K = 100_000.0  # turnover used by most cases: 1000 shares at 100


def bar(o=100.0, h=110.0, l=90.0, c=100.0) -> BarData:
    return BarData(timestamp="09:15", open=o, high=h, low=l, close=c, volume=1000.0)


# --------------------------------------------------------------------------
# Slippage
# --------------------------------------------------------------------------
def test_no_slippage_returns_the_price_unchanged():
    model = NoSlippage()
    for side in (OrderSide.BUY, OrderSide.SELL):
        assert model.compute(100.0, 10.0, side, bar()) == 100.0


def test_fixed_bps_slippage_is_a_symmetric_half_spread():
    model = FixedBpsSlippage(bps=5.0)
    # 5 bps of 100 = 0.05: paid worse on both sides, never better.
    assert model.compute(100.0, 1000.0, OrderSide.BUY, bar()) == pytest.approx(100.05)
    assert model.compute(100.0, 1000.0, OrderSide.SELL, bar()) == pytest.approx(99.95)


def test_fixed_bps_slippage_scales_with_price_not_quantity():
    model = FixedBpsSlippage(bps=10.0)
    assert model.compute(200.0, 1.0, OrderSide.BUY, bar()) == pytest.approx(200.2)
    assert model.compute(200.0, 1_000_000.0, OrderSide.BUY, bar()) == pytest.approx(200.2)


def test_volatility_slippage_is_proportional_to_the_bar_range():
    model = VolatilitySlippage(vol_factor=0.1)
    # (110 - 90) / 100 = 0.2 range; spread = 100 * 0.1 * 0.2 = 2.0
    assert model.compute(100.0, 10.0, OrderSide.BUY, bar()) == pytest.approx(102.0)
    assert model.compute(100.0, 10.0, OrderSide.SELL, bar()) == pytest.approx(98.0)
    # A tighter bar must cost strictly less slippage.
    tight = model.compute(100.0, 10.0, OrderSide.BUY, bar(h=101.0, l=99.0))
    assert tight == pytest.approx(100.2)
    assert tight < model.compute(100.0, 10.0, OrderSide.BUY, bar())


def test_volatility_slippage_degrades_to_the_price_on_an_unusable_bar():
    model = VolatilitySlippage(vol_factor=0.1)
    assert model.compute(100.0, 10.0, OrderSide.BUY, bar(c=math.nan)) == 100.0
    assert model.compute(100.0, 10.0, OrderSide.BUY, bar(c=0.0)) == 100.0


def test_slippage_builder_dispatches_on_the_configured_model():
    assert isinstance(build_slippage(SlippageConfig(model="none")), NoSlippage)
    assert isinstance(build_slippage(SlippageConfig(model="fixed_bps", bps=3.0)), FixedBpsSlippage)
    assert isinstance(build_slippage(SlippageConfig(model="volatility")), VolatilitySlippage)
    with pytest.raises(ValueError, match="unknown slippage model"):
        build_slippage(SlippageConfig(model="spread_table"))


# --------------------------------------------------------------------------
# Generic commission schemes
# --------------------------------------------------------------------------
def test_zero_commission_is_exactly_zero():
    assert ZeroCommission().compute(100.0, 1000.0, T100K, OrderSide.BUY) == 0.0


def test_per_share_commission_applies_its_floor_and_its_cap():
    model = PerShareCommission(cost_per_share=0.005, min_per_order=1.0, max_pct=0.005)
    assert model.compute(100.0, 1000.0, T100K) == pytest.approx(5.0)      # 1000 x 0.005
    assert model.compute(10.0, 100.0, 1000.0) == pytest.approx(1.0)       # floor binds
    capped = PerShareCommission(cost_per_share=0.01, min_per_order=1.0, max_pct=0.005)
    assert capped.compute(1.0, 1000.0, 1000.0) == pytest.approx(5.0)      # 0.5% of turnover
    no_cap = PerShareCommission(cost_per_share=0.01, min_per_order=0.0, max_pct=0.0)
    assert no_cap.compute(1.0, 1000.0, 1000.0) == pytest.approx(10.0)


def test_fixed_bps_commission_charges_the_floor():
    model = FixedBpsCommission(bps=1.0, min_per_order=0.5)
    assert model.compute(100.0, 1000.0, T100K) == pytest.approx(10.0)     # 1 bps of 100k
    assert model.compute(1.0, 100.0, 100.0) == pytest.approx(0.5)         # floor binds


def test_commission_builder_can_disable_costs():
    assert isinstance(build_commission(include_costs=False), ZeroCommission)
    assert isinstance(build_commission(), IndiaEquityCommission)


# --------------------------------------------------------------------------
# Itemised NSE schedule
# --------------------------------------------------------------------------
def test_delivery_buy_breakdown_matches_hand_calculation():
    parts = IndiaEquityCommission(IndiaEquityCosts()).breakdown(
        price=100.0, quantity=1000.0, notional=T100K, side=OrderSide.BUY)
    assert parts["brokerage"] == pytest.approx(20.0)        # cap binds: 30 -> 20
    assert parts["stt"] == pytest.approx(100.0)             # 0.1% both legs
    assert parts["exchange"] == pytest.approx(2.97)         # 0.0000297
    assert parts["sebi"] == pytest.approx(0.10)             # 1 rupee per crore
    assert parts["stamp_duty"] == pytest.approx(15.0)       # 0.015% buy leg
    assert parts["gst"] == pytest.approx(4.1526)            # 18% of 23.07
    assert parts["dp_charge"] == 0.0                        # buy leg
    assert parts["total"] == pytest.approx(142.2226)


def test_delivery_sell_breakdown_adds_the_dp_charge_and_drops_stamp():
    parts = IndiaEquityCommission(IndiaEquityCosts()).breakdown(
        price=100.0, quantity=1000.0, notional=T100K, side=OrderSide.SELL)
    assert parts["stamp_duty"] == 0.0
    assert parts["dp_charge"] == pytest.approx(13.5)
    assert parts["stt"] == pytest.approx(100.0)
    assert parts["total"] == pytest.approx(140.7226)


def test_intraday_buy_shifts_stt_off_and_stamp_duty_down():
    parts = IndiaEquityCommission(IndiaEquityCosts(mode="intraday")).breakdown(
        price=100.0, quantity=1000.0, notional=T100K, side=OrderSide.BUY)
    assert parts["stt"] == 0.0                              # no STT on intraday buy
    assert parts["stamp_duty"] == pytest.approx(3.0)        # 0.003% not 0.015%
    assert parts["dp_charge"] == 0.0
    assert parts["total"] == pytest.approx(30.2226)


def test_intraday_sell_pays_the_sell_leg_stt_and_no_dp():
    parts = IndiaEquityCommission(IndiaEquityCosts(mode="intraday")).breakdown(
        price=100.0, quantity=1000.0, notional=T100K, side=OrderSide.SELL)
    assert parts["stt"] == pytest.approx(25.0)              # 0.025% sell leg
    assert parts["stamp_duty"] == 0.0
    assert parts["dp_charge"] == 0.0                        # delivery-only charge
    assert parts["total"] == pytest.approx(52.2226)


def test_brokerage_cap_binds_only_above_the_crossover_turnover():
    model = IndiaEquityCommission(IndiaEquityCosts())
    small = model.breakdown(10.0, 1000.0, 10_000.0, OrderSide.BUY)
    assert small["brokerage"] == pytest.approx(3.0)         # 0.03% of 10k
    at_cap = model.breakdown(100.0, 667.0, 66_700.0, OrderSide.BUY)
    assert at_cap["brokerage"] == pytest.approx(20.0)       # 20.01% -> capped


def test_gst_base_excludes_statutory_charges_it_cannot_apply_to():
    cfg = IndiaEquityCosts()
    parts = IndiaEquityCommission(cfg).breakdown(100.0, 1000.0, T100K, OrderSide.BUY)
    expected_gst = (parts["brokerage"] + parts["exchange"] + parts["sebi"]) * cfg.gst_pct
    assert parts["gst"] == pytest.approx(expected_gst)
    assert parts["gst"] < (T100K * cfg.stt_delivery_pct) * cfg.gst_pct  # never on STT


def test_total_equals_the_sum_of_the_reported_components():
    model = IndiaEquityCommission(IndiaEquityCosts())
    for mode in ("delivery", "intraday"):
        for side in (OrderSide.BUY, OrderSide.SELL):
            parts = model.breakdown(100.0, 1000.0, T100K, side)
            components = {k: v for k, v in parts.items() if k != "total"}
            assert parts["total"] == pytest.approx(sum(components.values()))
            assert all(v >= 0.0 for v in components.values()), (mode, side)


def test_compute_matches_breakdown_and_records_history():
    model = IndiaEquityCommission(IndiaEquityCosts())
    total = model.compute(100.0, 1000.0, T100K, OrderSide.BUY)
    assert total == pytest.approx(model.breakdown(100.0, 1000.0, T100K, OrderSide.BUY)["total"])
    assert len(model.history) == 1
    assert model.history[0]["total"] == pytest.approx(total)


def test_cost_model_is_fully_configurable():
    cfg = IndiaEquityCosts(brokerage_pct=0.0, brokerage_flat=0.0, stt_delivery_pct=0.0,
                           exchange_txn_pct=0.0, sebi_turnover_pct=0.0,
                           stamp_duty_buy_pct=0.0, gst_pct=0.0, dp_charge_per_sell=0.0)
    parts = IndiaEquityCommission(cfg).breakdown(100.0, 1000.0, T100K, OrderSide.SELL)
    assert parts["total"] == 0.0
