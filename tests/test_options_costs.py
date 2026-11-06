"""P2 stage 5 / P2.1 - options-specific transaction cost engine.

Every expected rupee is written out by hand from the documented component rules, so a
change to a rate or a taxable base fails a test rather than silently moving a number.

P2.1 replaced the fabricated ``Rs 20 per lot`` exchange fee with charges on *premium
turnover* and added the NSE IPFT contribution, so the tests here pin:
  * each historical rate boundary (STT / exchange txn / IPFT effective dates);
  * the premium-turnover calculation basis (turnover, not a per-lot count);
  * buy/sell asymmetry (STT on the premium seller, stamp duty on the buyer);
  * multiple lots and partial exits (prorated on turnover);
  * the configurable brokerage rule (strict validation needs a *verified* broker config
    or explicit exclusion);
  * GST on the taxable fee base (brokerage + exchange + IPFT + SEBI);
  * a complete round trip and the recalculated worked example.

The arithmetic tests use an explicitly *sourced* schedule (estimated=False).  A separate
group uses the real default schedule to pin the honesty contract per trade date: the
Oct-2024 and later eras are fully sourced (P2.1.2 verified the 2024-10-01 exchange rate
from the primary circular NSE/FA/64232), while the two slab-era exchange-transaction
rows stay estimated and exclude those trades.  A third group pins the P2.1.2
verification levels: statutory_verified vs all_in_verified must not be conflated -
brokerage exclusion is never all-in verification.
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
    """A hand-checkable premium-turnover schedule, every rate marked independently
    sourced.  It deliberately omits a ``gst`` row so the engine's config fallback
    (``IndiaOptionsCosts.gst_pct``) is exercised too."""
    return (
        OptionsChargeRegime("stt", "premium_pct", 0.001, "2000-01-01", None,
                            side="sell", basis="premium_turnover",
                            provenance="circular X", verification="verified",
                            estimated=False),
        OptionsChargeRegime("exchange_txn", "premium_pct", 0.0005, "2000-01-01", None,
                            basis="premium_turnover", provenance="circular X",
                            verification="verified", estimated=False),
        OptionsChargeRegime("ipft", "premium_pct", 0.000005, "2000-01-01", None,
                            basis="premium_turnover", provenance="circular X",
                            verification="verified", estimated=False),
        OptionsChargeRegime("sebi", "premium_pct", 0.000001, "2000-01-01", None,
                            basis="premium_turnover", provenance="circular X",
                            verification="verified", estimated=False),
        OptionsChargeRegime("stamp_duty", "premium_pct", 0.00003, "2000-01-01", None,
                            side="buy", basis="premium_turnover", provenance="circular X",
                            verification="verified", estimated=False),
    )


def sourced_commission(**kw) -> IndiaOptionsCommission:
    cfg = IndiaOptionsCosts(regimes=sourced_schedule(), **kw)
    return IndiaOptionsCommission(cfg)


# ---------------------------------------------------------------------------
# Hand-calculated legs (premium=100, lot=10, lots=2 -> turnover 2000)
#   exchange = 2000 * 0.0005   = 1.0
#   ipft     = 2000 * 0.000005 = 0.01
#   sebi     = 2000 * 0.000001 = 0.002
#   stamp    = 2000 * 0.00003  = 0.06   (buy only)
#   stt      = 2000 * 0.001    = 2.0    (sell only)
#   gst base = brokerage + exchange + ipft + sebi = 0 + 1.0 + 0.01 + 0.002 = 1.012
#   gst      = 1.012 * 0.18    = 0.18216
#   entry total = 1.0 + 0.01 + 0.002 + 0.06 + 0.18216 = 1.25416
#   exit  total = 2.0 + 1.0 + 0.01 + 0.002 + 0.18216   = 3.19416
# ---------------------------------------------------------------------------
def test_entry_leg_components_match_a_hand_calculation():
    parts = sourced_commission().breakdown(100.0, 10, 2, OrderSide.BUY, "2025-01-02")
    assert parts["brokerage"] == 0.0
    assert parts["stt"] == 0.0                        # STT is a premium-seller charge
    assert parts["exchange"] == pytest.approx(1.0)    # turnover 2000 * 0.0005
    assert parts["ipft"] == pytest.approx(0.01)       # turnover 2000 * 5e-6
    assert parts["sebi"] == pytest.approx(0.002)
    assert parts["stamp_duty"] == pytest.approx(0.06)
    assert parts["gst"] == pytest.approx(0.18216, abs=1e-9)
    assert sum(parts.values()) == pytest.approx(1.25416, abs=1e-9)


def test_exit_leg_components_match_a_hand_calculation():
    parts = sourced_commission().breakdown(100.0, 10, 2, OrderSide.SELL, "2025-01-30")
    assert parts["stt"] == pytest.approx(2.0)         # sell leg pays STT on premium
    assert parts["stamp_duty"] == 0.0                 # stamp is a buyer-only charge
    assert parts["exchange"] == pytest.approx(1.0)
    assert parts["ipft"] == pytest.approx(0.01)
    assert sum(parts.values()) == pytest.approx(3.19416, abs=1e-9)


def test_breakdown_is_itemised_and_carries_no_total():
    parts = sourced_commission().breakdown(100.0, 10, 2, OrderSide.BUY, "2025-01-02")
    assert "total" not in parts
    assert set(parts) == {"brokerage", "stt", "exchange", "ipft", "sebi",
                          "stamp_duty", "gst", "slippage"}


def test_compute_totals_the_breakdown():
    comm = sourced_commission()
    parts = comm.breakdown(100.0, 10, 2, OrderSide.SELL, "2025-01-30")
    assert comm.compute(100.0, 10, 2, OrderSide.SELL, "2025-01-30") == pytest.approx(
        sum(parts.values()))


def test_no_dp_charge_exists_for_options():
    parts = sourced_commission().breakdown(100.0, 10, 2, OrderSide.SELL, "2025-01-30")
    assert "dp" not in parts and "dp_charge" not in parts


# ---------------------------------------------------------------------------
# Premium-turnover basis (NOT a fabricated flat per-lot fee)
# ---------------------------------------------------------------------------
def test_exchange_charge_is_on_turnover_not_on_lot_count():
    comm = sourced_commission()
    # same turnover (2000) reached two ways -> identical exchange charge
    a = comm.breakdown(100.0, 10, 2, OrderSide.BUY, "2025-01-02")   # 100*10*2
    b = comm.breakdown(200.0, 10, 1, OrderSide.BUY, "2025-01-02")   # 200*10*1
    assert a["exchange"] == pytest.approx(b["exchange"])
    # double the turnover -> double the charge (a per-lot fee would not scale with premium)
    c = comm.breakdown(100.0, 10, 4, OrderSide.BUY, "2025-01-02")   # 100*10*4 = 4000
    assert c["exchange"] == pytest.approx(2 * a["exchange"])
    assert a["exchange"] == pytest.approx(2000 * 0.0005)


def test_multiple_lots_scale_every_turnover_component():
    comm = sourced_commission()
    one = comm.breakdown(100.0, 10, 1, OrderSide.SELL, "2025-01-30")
    two = comm.breakdown(100.0, 10, 2, OrderSide.SELL, "2025-01-30")
    for comp in ("stt", "exchange", "ipft", "sebi", "gst", "slippage"):
        assert two[comp] == pytest.approx(2 * one[comp])
    assert sum(two.values()) == pytest.approx(2 * sum(one.values()))


def test_partial_exit_prorates_on_turnover():
    comm = sourced_commission()
    full = comm.breakdown(100.0, 10, 2, OrderSide.SELL, "2025-01-30")
    half = comm.breakdown(100.0, 10, 1, OrderSide.SELL, "2025-01-30")
    assert half["stt"] == pytest.approx(full["stt"] / 2)
    assert half["exchange"] == pytest.approx(full["exchange"] / 2)
    assert sum(half.values()) == pytest.approx(sum(full.values()) / 2)


# ---------------------------------------------------------------------------
# Buy / sell asymmetry
# ---------------------------------------------------------------------------
def test_stt_falls_on_the_seller_and_stamp_on_the_buyer():
    comm = sourced_commission()
    buy = comm.breakdown(100.0, 10, 2, OrderSide.BUY, "2025-01-02")
    sell = comm.breakdown(100.0, 10, 2, OrderSide.SELL, "2025-01-30")
    assert buy["stt"] == 0.0 and buy["stamp_duty"] > 0.0
    assert sell["stt"] > 0.0 and sell["stamp_duty"] == 0.0
    # exchange txn, IPFT and SEBI are charged each side
    assert buy["exchange"] == pytest.approx(sell["exchange"])
    assert buy["ipft"] == pytest.approx(sell["ipft"])
    assert buy["sebi"] == pytest.approx(sell["sebi"])


# ---------------------------------------------------------------------------
# Effective-date boundaries switch rates (every historical boundary)
# ---------------------------------------------------------------------------
def test_stt_boundaries():
    regs = default_options_charge_regimes()
    assert resolve_rate(regs, "stt", "2024-09-30").value == pytest.approx(0.000625)
    assert resolve_rate(regs, "stt", "2024-10-01").value == pytest.approx(0.001)
    assert resolve_rate(regs, "stt", "2026-03-31").value == pytest.approx(0.001)
    assert resolve_rate(regs, "stt", "2026-04-01").value == pytest.approx(0.0015)


def test_exchange_txn_boundaries():
    regs = default_options_charge_regimes()
    assert resolve_rate(regs, "exchange_txn", "2023-03-31").value == pytest.approx(0.00053)
    assert resolve_rate(regs, "exchange_txn", "2023-04-01").value == pytest.approx(0.0005)
    assert resolve_rate(regs, "exchange_txn", "2024-09-30").value == pytest.approx(0.0005)
    assert resolve_rate(regs, "exchange_txn", "2024-10-01").value == pytest.approx(0.0003503)
    assert resolve_rate(regs, "exchange_txn", "2026-02-28").value == pytest.approx(0.0003503)
    assert resolve_rate(regs, "exchange_txn", "2026-03-01").value == pytest.approx(0.000355299)


def test_ipft_boundaries():
    regs = default_options_charge_regimes()
    assert resolve_rate(regs, "ipft", "2023-03-31").value == pytest.approx(1e-9)
    assert resolve_rate(regs, "ipft", "2023-04-01").value == pytest.approx(5e-6)
    assert resolve_rate(regs, "ipft", "2026-02-28").value == pytest.approx(5e-6)
    assert resolve_rate(regs, "ipft", "2026-03-01").value == pytest.approx(1e-9)


def test_breakdown_uses_the_rate_in_force_on_the_trade_date():
    comm = IndiaOptionsCommission()  # default schedule
    pre = comm.breakdown(100.0, 10, 1, OrderSide.SELL, "2024-09-30")["stt"]
    post = comm.breakdown(100.0, 10, 1, OrderSide.SELL, "2024-10-01")["stt"]
    assert pre == pytest.approx(1000 * 0.000625)
    assert post == pytest.approx(1000 * 0.001)


def test_a_date_before_any_regime_raises_rather_than_guessing():
    with pytest.raises(UnknownChargeError):
        resolve_rate(default_options_charge_regimes(), "stt", "2010-01-01")
    with pytest.raises(UnknownChargeError):
        resolve_rate(default_options_charge_regimes(), "exchange_txn", "2020-06-01")


# ---------------------------------------------------------------------------
# GST is 18% on the taxable fee base (brokerage + exchange + IPFT + SEBI)
# ---------------------------------------------------------------------------
def test_gst_base_includes_brokerage_exchange_ipft_and_sebi():
    comm = sourced_commission(brokerage=BrokerageConfig(model="flat", flat_per_order=20.0))
    parts = comm.breakdown(100.0, 10, 2, OrderSide.BUY, "2025-01-02")   # turnover 2000
    base = 20.0 + parts["exchange"] + parts["ipft"] + parts["sebi"]     # 20 + 1.0 + 0.01 + 0.002
    assert parts["gst"] == pytest.approx(base * 0.18, abs=1e-9)
    # STT and stamp duty are statutory taxes, not part of the GST-on-fees base
    assert parts["gst"] == pytest.approx((20 + 1.0 + 0.01 + 0.002) * 0.18, abs=1e-9)


def test_gst_falls_back_to_config_when_the_schedule_has_no_gst_row():
    comm = IndiaOptionsCommission(IndiaOptionsCosts(regimes=sourced_schedule(), gst_pct=0.12))
    parts = comm.breakdown(100.0, 10, 2, OrderSide.BUY, "2025-01-02")
    assert parts["gst"] == pytest.approx((1.0 + 0.01 + 0.002) * 0.12, abs=1e-9)


# ---------------------------------------------------------------------------
# Brokerage is configurable; strict validation needs it verified or excluded
# ---------------------------------------------------------------------------
def test_unconfigured_brokerage_is_neutral_zero_and_excluded():
    comm = IndiaOptionsCommission(IndiaOptionsCosts(regimes=sourced_schedule()))
    parts = comm.breakdown(100.0, 10, 2, OrderSide.BUY, "2025-01-02")
    assert parts["brokerage"] == 0.0
    result = comm.assess(100.0, 10, 2, OrderSide.BUY, "2025-01-02")
    assert result.brokerage_excluded is True


def test_flat_brokerage_adds_once_per_leg_and_flows_to_gst():
    comm = sourced_commission(brokerage=BrokerageConfig(model="flat", flat_per_order=20.0))
    parts = comm.breakdown(100.0, 10, 2, OrderSide.BUY, "2025-01-02")
    assert parts["brokerage"] == 20.0
    assert parts["gst"] == pytest.approx((20 + 1.0 + 0.01 + 0.002) * 0.18, abs=1e-9)


def test_percentage_brokerage_respects_its_cap():
    comm = sourced_commission(brokerage=BrokerageConfig(
        model="percentage", percentage=0.01, cap_percentage=0.001))
    parts = comm.breakdown(100.0, 10, 2, OrderSide.BUY, "2025-01-02")   # turnover 2000
    assert parts["brokerage"] == pytest.approx(2000 * 0.001)            # capped, not 1%


def test_unverified_brokerage_blocks_strict_verification():
    comm = IndiaOptionsCommission(IndiaOptionsCosts(
        brokerage=BrokerageConfig(model="flat", flat_per_order=20.0, verified=False)))
    result = comm.assess(150.0, 75, 2, OrderSide.BUY, "2026-03-02")   # all rates sourced
    assert result.brokerage_excluded is False
    assert result.brokerage_verified is False
    assert result.strict_verifiable is False


def test_verified_brokerage_permits_strict_verification():
    comm = IndiaOptionsCommission(IndiaOptionsCosts(
        brokerage=BrokerageConfig(model="flat", flat_per_order=20.0, verified=True)))
    result = comm.assess(150.0, 75, 2, OrderSide.BUY, "2026-03-02")
    assert result.brokerage_verified is True
    assert result.strict_verifiable is True


# ---------------------------------------------------------------------------
# P2.1.2 verification levels: statutory vs all-in must not be conflated
# ---------------------------------------------------------------------------
def test_brokerage_excluded_is_statutory_verified_but_not_all_in():
    """Directive §3: omitting the broker fee never makes a cost 'fully all-in net'."""
    result = IndiaOptionsCommission().assess(150.0, 75, 1, OrderSide.BUY, "2026-03-02")
    assert result.statutory_verified is True
    assert result.brokerage_excluded is True
    assert result.all_in_verified is False
    assert result.strict_verifiable is True          # publishing-safe in this config
    assert "brokerage excluded" in result.cost_basis
    assert "do not label as net profit" in result.cost_basis


def test_attested_brokerage_is_the_only_all_in_verified_state():
    comm = IndiaOptionsCommission(IndiaOptionsCosts(
        brokerage=BrokerageConfig(model="flat", flat_per_order=20.0, verified=True)))
    result = comm.assess(150.0, 75, 1, OrderSide.BUY, "2026-03-02")
    assert result.statutory_verified is True
    assert result.all_in_verified is True
    assert result.cost_basis.startswith("all-in verified")


def test_unverified_brokerage_is_neither_all_in_nor_publishing_safe():
    comm = IndiaOptionsCommission(IndiaOptionsCosts(
        brokerage=BrokerageConfig(model="percentage", percentage=0.001)))
    result = comm.assess(150.0, 75, 1, OrderSide.BUY, "2026-03-02")
    assert result.statutory_verified is True         # statutory side fully sourced
    assert result.all_in_verified is False
    assert result.strict_verifiable is False
    assert "unverified" in result.cost_basis


def test_slab_era_leg_is_neither_statutory_nor_all_in_verified():
    result = IndiaOptionsCommission().assess(150.0, 75, 1, OrderSide.BUY, "2023-05-05")
    assert result.statutory_verified is False
    assert result.all_in_verified is False
    assert result.strict_verifiable is False
    assert result.cost_basis.startswith("modelling only")


# ---------------------------------------------------------------------------
# The honesty contract, per trade date, on the real default schedule
# ---------------------------------------------------------------------------
def test_default_schedule_sources_every_in_force_core_rate():
    regs = default_options_charge_regimes()
    core = {"stt", "exchange_txn", "ipft", "sebi", "stamp_duty"}
    live = [r for r in regs if r.component in core and r.effective_to is None]
    assert live and all(not r.estimated for r in live)
    # P2.1.2: only the two slab-era exchange-txn rows stay estimated (slab convention);
    # the 2024-10-01 flat era is verified from the primary circular NSE/FA/64232.
    exch_est = [r for r in regs if r.component == "exchange_txn" and r.estimated]
    assert len(exch_est) == 2
    assert all(r.effective_to <= "2024-09-30" for r in exch_est)
    flat_2024 = [r for r in regs if r.component == "exchange_txn"
                 and r.effective_from == "2024-10-01"]
    assert flat_2024 and not flat_2024[0].estimated
    assert "64232" in flat_2024[0].circular_ref


def test_a_current_era_leg_is_strict_verifiable_when_brokerage_excluded():
    result = IndiaOptionsCommission().assess(150.0, 75, 1, OrderSide.BUY, "2026-03-02")
    assert result.total > 0
    assert result.estimated_components == ()
    assert result.strict_verifiable is True


def test_a_slab_era_leg_is_costed_but_not_strict_verifiable():
    result = IndiaOptionsCommission().assess(150.0, 75, 1, OrderSide.BUY, "2022-01-03")
    assert result.total > 0
    assert result.strict_verifiable is False
    assert result.estimated_components == ("exchange_txn",)


def test_a_post_oct_2024_leg_is_strict_verifiable_after_the_fa64232_source():
    """P2.1.2: the 2024-10-01 exchange era became verified (primary circular FA64232)."""
    result = IndiaOptionsCommission().assess(150.0, 75, 1, OrderSide.BUY, "2025-01-02")
    assert result.total > 0
    assert result.estimated_components == ()
    assert result.strict_verifiable is True


def test_a_sourced_schedule_leg_is_strict_verifiable():
    result = sourced_commission().assess(100.0, 10, 1, OrderSide.BUY, "2025-01-02")
    assert result.strict_verifiable is True
    assert result.estimated_components == ()


# ---------------------------------------------------------------------------
# Complete round trips
# ---------------------------------------------------------------------------
def test_round_trip_reports_the_strict_verdict_and_total():
    trip = sourced_commission().round_trip(100.0, 120.0, 10, 2, "2025-01-02", "2025-01-30")
    assert trip["strict_verifiable"] is True
    assert trip["brokerage_excluded"] is True
    assert trip["total_costs"] == pytest.approx(trip["entry"].total + trip["exit"].total)


def test_worked_example_2026_round_trip_is_strict_verifiable():
    """Recalculated worked example: 150 -> 180 premium, lot 75, 2 lots, current era."""
    trip = IndiaOptionsCommission().round_trip(150.0, 180.0, 75, 2,
                                               "2026-03-02", "2026-03-20")
    # entry: turnover 22500 -> exchange 7.9942275, ipft 0.0000225, sebi 0.0225,
    #        stamp 0.675, gst (8.01675)*0.18 = 1.443015  => 10.134765
    # exit : turnover 27000 -> stt 27.0, exchange 9.593073, ipft 0.000027,
    #        sebi 0.027, gst (9.6201)*0.18 = 1.731618     => 38.351718
    assert trip["entry"].total == pytest.approx(10.134765, abs=1e-6)
    assert trip["exit"].total == pytest.approx(38.351718, abs=1e-6)
    assert trip["total_costs"] == pytest.approx(48.486483, abs=1e-5)
    assert trip["strict_verifiable"] is True


def test_worked_example_2025_round_trip_is_strict_verifiable():
    """P2.1.2: the 2025 trade sits in the Oct-2024 flat era, now sourced from FA64232."""
    trip = IndiaOptionsCommission().round_trip(150.0, 180.0, 75, 2,
                                               "2025-01-02", "2025-01-30")
    assert trip["total_costs"] > 0
    assert trip["strict_verifiable"] is True
    assert trip["statutory_verified"] is True
    # brokerage excluded is NOT all-in verification (the fee is omitted, not sourced)
    assert trip["all_in_verified"] is False
    assert "do not label as net profit" in trip["cost_basis"]


def test_worked_example_slab_era_round_trip_is_not_strict_verifiable():
    """A 2022 trade hits the estimated slab convention and stays fail-closed."""
    trip = IndiaOptionsCommission().round_trip(150.0, 180.0, 25, 2,
                                               "2022-01-03", "2022-01-27")
    assert trip["total_costs"] > 0
    assert trip["strict_verifiable"] is False
    assert trip["statutory_verified"] is False
    assert trip["cost_basis"].startswith("modelling only")


# ---------------------------------------------------------------------------
# Exercised-option STT is reference-only (the engine has no exercise path)
# ---------------------------------------------------------------------------
def test_exercise_stt_is_available_but_not_folded_into_breakdown():
    comm = IndiaOptionsCommission()
    # purchaser STT on intrinsic value: 0.125% upto 2026-03-31, 0.15% wef 2026-04-01
    assert comm.exercise_stt(1000.0, "2025-01-02") == pytest.approx(1.25)
    assert comm.exercise_stt(1000.0, "2026-04-01") == pytest.approx(1.5)
    # it never appears as a component of a normal (premium-sale) leg
    parts = comm.breakdown(150.0, 75, 1, OrderSide.SELL, "2026-03-20")
    assert "stt_exercised" not in parts and "exercise_stt" not in parts


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("lots,lot_size", [(0, 10), (-1, 10), (2, 0), (2, -5)])
def test_non_positive_quantity_is_rejected(lots, lot_size):
    with pytest.raises(ValueError, match="must be positive"):
        sourced_commission().breakdown(100.0, lot_size, lots, OrderSide.BUY, "2025-01-02")
