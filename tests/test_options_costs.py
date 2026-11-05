"""P2 stage 5 - options-specific transaction cost engine.

Every expected rupee is written out by hand from the documented component rules, so a
change to a rate or a taxable base fails a test rather than silently moving a number.
The tests use an explicitly *sourced* schedule (estimated=False) to verify the arithmetic,
then a separate group uses the real offline default (estimated=True) to pin the honesty
contract: unverified rates are flagged and the trade is excluded from strict validation.
"""

from __future__ import annotations

import pytest

from nifty_quant.config import (
    BrokerageConfig, IndiaOptionsCosts, OptionsChargeRegime,
    default_options_charge_regimes)
from nifty_quant.engine.orders import OrderSide
from nifty_quant.engine.options_costs import (
    IndiaOptionsCommission, UnknownChargeError, resolve_rate)


def sourced_schedule() -> tuple[OptionsChargeRegime, ...]:
    """A hand-checkable schedule with every rate marked independently sourced."""
    return (
        OptionsChargeRegime("stt", "premium_pct", 0.001, "2000-01-01", None,
                            side="sell", provenance="circular X", estimated=False),
        OptionsChargeRegime("exchange_txn", "per_lot", 5.0, "2000-01-01", None,
                            provenance="circular X", estimated=False),
        OptionsChargeRegime("sebi", "premium_pct", 0.000001, "2000-01-01", None,
                            provenance="circular X", estimated=False),
        OptionsChargeRegime("stamp_duty", "premium_pct", 0.00003, "2000-01-01", None,
                            side="buy", provenance="circular X", estimated=False),
    )


def sourced_commission(**kw) -> IndiaOptionsCommission:
    cfg = IndiaOptionsCosts(regimes=sourced_schedule(), **kw)
    return IndiaOptionsCommission(cfg)


# premium=100, lot=10, lots=2 -> turnover 2000
# entry (buy): exchange 10, sebi 0.002, stamp 0.06, gst=(0+10+0.002)*0.18=1.80036 -> 11.86236
# exit  (sell): stt 2.0, exchange 10, sebi 0.002, gst 1.80036 -> 13.80236
def test_entry_leg_components_match_a_hand_calculation():
    parts = sourced_commission().breakdown(100.0, 10, 2, OrderSide.BUY, "2025-01-02")
    assert parts["brokerage"] == 0.0
    assert parts["stt"] == 0.0                       # STT is a premium-seller charge
    assert parts["exchange"] == pytest.approx(10.0)  # 5 per lot x 2 lots
    assert parts["sebi"] == pytest.approx(0.002)
    assert parts["stamp_duty"] == pytest.approx(0.06)
    assert parts["gst"] == pytest.approx(1.80036, abs=1e-6)
    assert sum(parts.values()) == pytest.approx(11.86236, abs=1e-5)


def test_exit_leg_components_match_a_hand_calculation():
    parts = sourced_commission().breakdown(100.0, 10, 2, OrderSide.SELL, "2025-01-30")
    assert parts["stt"] == pytest.approx(2.0)        # sell leg pays STT
    assert parts["stamp_duty"] == 0.0                # stamp is a buyer-only charge
    assert sum(parts.values()) == pytest.approx(13.80236, abs=1e-5)


def test_breakdown_is_itemised_and_carries_no_total():
    parts = sourced_commission().breakdown(100.0, 10, 2, OrderSide.BUY, "2025-01-02")
    assert "total" not in parts
    assert set(parts) == {"brokerage", "stt", "exchange", "sebi",
                          "stamp_duty", "gst", "slippage"}


def test_compute_totals_the_breakdown():
    comm = sourced_commission()
    parts = comm.breakdown(100.0, 10, 2, OrderSide.SELL, "2025-01-30")
    assert comm.compute(100.0, 10, 2, OrderSide.SELL, "2025-01-30") == pytest.approx(
        sum(parts.values()))


def test_no_dp_charge_exists_for_options():
    parts = sourced_commission().breakdown(100.0, 10, 2, OrderSide.SELL, "2025-01-30")
    assert "dp" not in parts and "dp_charge" not in parts


def test_partial_exit_prorates_on_lots():
    comm = sourced_commission()
    full = comm.breakdown(100.0, 10, 2, OrderSide.SELL, "2025-01-30")
    half = comm.breakdown(100.0, 10, 1, OrderSide.SELL, "2025-01-30")
    assert half["exchange"] == pytest.approx(full["exchange"] / 2)   # per-lot x 1 lot
    assert half["stt"] == pytest.approx(full["stt"] / 2)             # turnover halves
    assert sum(half.values()) == pytest.approx(sum(full.values()) / 2)


# ---------------------------------------------------------------------------
# Effective-date boundaries switch rates
# ---------------------------------------------------------------------------
def test_stt_rate_rises_at_the_documented_effective_boundary():
    pre = resolve_rate(default_options_charge_regimes(), "stt", "2024-09-30")
    post = resolve_rate(default_options_charge_regimes(), "stt", "2024-10-01")
    assert pre.value == pytest.approx(0.0005)
    assert post.value == pytest.approx(0.001)


def test_breakdown_uses_the_rate_in_force_on_the_trade_date():
    comm = IndiaOptionsCommission()  # default (estimated) schedule
    pre = comm.breakdown(100.0, 10, 1, OrderSide.SELL, "2024-09-30")["stt"]
    post = comm.breakdown(100.0, 10, 1, OrderSide.SELL, "2024-10-01")["stt"]
    assert post == pytest.approx(pre * 2)


def test_a_date_before_any_regime_raises_rather_than_guessing():
    with pytest.raises(UnknownChargeError):
        resolve_rate(default_options_charge_regimes(), "stt", "2010-01-01")


# ---------------------------------------------------------------------------
# Brokerage is configurable and assumed nothing
# ---------------------------------------------------------------------------
def test_unconfigured_brokerage_is_neutral_zero():
    parts = IndiaOptionsCommission(IndiaOptionsCosts(
        regimes=sourced_schedule())).breakdown(100.0, 10, 2, OrderSide.BUY, "2025-01-02")
    assert parts["brokerage"] == 0.0


def test_flat_brokerage_adds_once_per_leg_and_flows_to_gst():
    comm = IndiaOptionsCommission(IndiaOptionsCosts(
        regimes=sourced_schedule(),
        brokerage=BrokerageConfig(model="flat", flat_per_order=20.0)))
    parts = comm.breakdown(100.0, 10, 2, OrderSide.BUY, "2025-01-02")
    assert parts["brokerage"] == 20.0
    # GST taxable base now includes brokerage: (20 + 10 + 0.002) * 0.18
    assert parts["gst"] == pytest.approx((20 + 10 + 0.002) * 0.18, abs=1e-6)


def test_percentage_brokerage_respects_its_cap():
    comm = IndiaOptionsCommission(IndiaOptionsCosts(
        regimes=sourced_schedule(),
        brokerage=BrokerageConfig(model="percentage", percentage=0.01,
                                  cap_percentage=0.001)))
    parts = comm.breakdown(100.0, 10, 2, OrderSide.BUY, "2025-01-02")  # turnover 2000
    assert parts["brokerage"] == pytest.approx(2000 * 0.001)           # capped, not 1%


# ---------------------------------------------------------------------------
# The honesty contract: unverified rates are flagged and excluded from strict use
# ---------------------------------------------------------------------------
def test_default_offline_schedule_is_fully_estimated():
    assert all(r.estimated for r in default_options_charge_regimes())


def test_a_default_leg_is_costed_but_not_strict_verifiable():
    result = IndiaOptionsCommission().assess(150.0, 75, 1, OrderSide.BUY, "2025-01-02")
    assert result.total > 0
    assert result.strict_verifiable is False
    assert set(result.estimated_components) == {"stt", "exchange_txn", "sebi", "stamp_duty"}


def test_a_sourced_leg_is_strict_verifiable():
    result = sourced_commission().assess(100.0, 10, 1, OrderSide.BUY, "2025-01-02")
    assert result.strict_verifiable is True
    assert result.estimated_components == ()


def test_round_trip_reports_the_strict_verdict_and_total():
    trip = sourced_commission().round_trip(100.0, 120.0, 10, 2,
                                           "2025-01-02", "2025-01-30")
    assert trip["strict_verifiable"] is True
    assert trip["total_costs"] == pytest.approx(trip["entry"].total + trip["exit"].total)


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("lots,lot_size", [(0, 10), (-1, 10), (2, 0), (2, -5)])
def test_non_positive_quantity_is_rejected(lots, lot_size):
    with pytest.raises(ValueError, match="must be positive"):
        sourced_commission().breakdown(100.0, lot_size, lots, OrderSide.BUY, "2025-01-02")
