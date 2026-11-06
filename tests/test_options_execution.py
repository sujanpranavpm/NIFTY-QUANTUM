"""Deterministic tests for the options execution engine (P2 stage 6, Part 7).

Every case is hand-checked against a hard-coded bar path - no dataset, no clock, no
randomness - so each fill price and reason can be verified by eye.  The suite pins the
properties the directive demands: next-bar entry, conservative stop-wins collision, worse
gap fills, refusal to trade a missing interval, expiry/time/trailing exits, the liquidity
gate, and the monotonic optimistic->conservative profitability ordering.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from nifty_quant.config import BASELINE, CONSERVATIVE, OPTIMISTIC, ExecutionScenario
from nifty_quant.engine.options_costs import IndiaOptionsCommission
from nifty_quant.engine.options_execution import (
    FILLED,
    NO_ENTRY,
    OPEN_AT_END,
    REASON_END,
    REASON_EXPIRY,
    REASON_STOP,
    REASON_TARGET,
    REASON_TIME,
    REASON_TRAIL,
    UNRESOLVED,
    ExitRules,
    OptionBar,
    OptionsExecutionEngine,
)


def ts(minutes: int) -> datetime:
    return datetime(2025, 1, 2, 9, 15 + minutes)


def mk(minutes, o, h, l, c, vol=1_000_000.0, *, available=True, expiry=False) -> OptionBar:
    if not available:
        return OptionBar(ts(minutes), float("nan"), float("nan"), float("nan"),
                         float("nan"), 0.0, available=False, is_expiry=expiry)
    return OptionBar(ts(minutes), o, h, l, c, vol, available=True, is_expiry=expiry)


ENGINE = OptionsExecutionEngine()  # no commission -> pure execution behaviour


def run(bars, rules, *, lots=1, lot_size=75, scenario=OPTIMISTIC, signal=0, engine=ENGINE):
    return engine.run(bars, signal, rules, lots=lots, lot_size=lot_size, scenario=scenario)


# ---------------------------------------------------------------------------
# Next-bar entry
# ---------------------------------------------------------------------------
def test_entry_is_at_the_next_bar_open_not_the_signal_close():
    bars = [mk(0, 100, 101, 99, 100.5),    # signal bar closes at 100.5
           mk(5, 101, 102, 100, 101.5),    # next bar opens at 101 -> entry
           mk(10, 102, 121, 101, 120)]
    r = run(bars, ExitRules(target_premium=120.0))
    assert r.entry_premium == pytest.approx(101.0)   # the NEXT open, not the 100.5 close
    assert r.entry_timestamp == ts(5)


def test_a_signal_on_the_last_bar_never_enters():
    bars = [mk(0, 100, 101, 99, 100), mk(5, 100, 101, 99, 100)]
    r = run(bars, ExitRules(target_premium=120.0), signal=1)
    assert r.status == NO_ENTRY and not r.entered


def test_entry_into_a_missing_interval_is_refused():
    bars = [mk(0, 100, 101, 99, 100), mk(5, 0, 0, 0, 0, available=False)]
    r = run(bars, ExitRules(target_premium=120.0))
    assert r.status == NO_ENTRY and r.entry_premium is None


# ---------------------------------------------------------------------------
# Conservative collision + gap handling
# ---------------------------------------------------------------------------
def test_stop_wins_when_one_bar_touches_both_stop_and_target():
    bars = [mk(0, 100, 101, 99, 100), mk(5, 100, 101, 99, 100),
            mk(10, 100, 125, 85, 110)]   # low<=90 and high>=120 in the same bar
    r = run(bars, ExitRules(stop_premium=90.0, target_premium=120.0))
    assert r.reason == REASON_STOP
    assert r.exit_premium == pytest.approx(90.0)      # never credited the 120 target


def test_a_stop_that_gaps_through_fills_at_the_worse_open():
    bars = [mk(0, 100, 101, 99, 100), mk(5, 100, 101, 99, 100),
            mk(10, 80, 82, 78, 81)]      # opens below the 90 stop
    r = run(bars, ExitRules(stop_premium=90.0))
    assert r.reason == REASON_STOP
    assert r.exit_premium == pytest.approx(80.0)      # min(stop, open) = the gap-down open


def test_a_target_earns_no_favourable_gap_credit():
    bars = [mk(0, 100, 101, 99, 100), mk(5, 100, 101, 99, 100),
            mk(10, 130, 135, 129, 133)]  # opens above the 120 target
    r = run(bars, ExitRules(target_premium=120.0))
    assert r.reason == REASON_TARGET
    assert r.exit_premium == pytest.approx(120.0)     # the limit price, not the 130 open


# ---------------------------------------------------------------------------
# Missing data at exit
# ---------------------------------------------------------------------------
def test_expiry_on_a_missing_bar_is_unresolved_not_fabricated():
    bars = [mk(0, 100, 101, 99, 100), mk(5, 100, 104, 100, 103),
            mk(10, 0, 0, 0, 0, available=False, expiry=True)]
    r = run(bars, ExitRules(stop_premium=90.0, target_premium=120.0))
    assert r.status == UNRESOLVED and r.exit_premium is None


# ---------------------------------------------------------------------------
# Expiry / time / trailing exits
# ---------------------------------------------------------------------------
def test_expiry_settles_at_the_final_close():
    bars = [mk(0, 100, 101, 99, 100), mk(5, 100, 104, 100, 103),
            mk(10, 108, 112, 107, 110, expiry=True)]
    r = run(bars, ExitRules(stop_premium=50.0, target_premium=200.0))
    assert r.reason == REASON_EXPIRY and r.exit_premium == pytest.approx(110.0)


def test_time_based_exit_fires_after_max_hold_bars():
    bars = [mk(0, 100, 101, 99, 100), mk(5, 100, 104, 100, 103),   # entry
            mk(10, 104, 106, 103, 105),                            # held 1
            mk(15, 105, 107, 104, 106)]                            # held 2 -> time exit
    r = run(bars, ExitRules(stop_premium=50.0, target_premium=200.0, max_hold_bars=2))
    assert r.reason == REASON_TIME and r.exit_premium == pytest.approx(106.0)


def test_trailing_stop_anchors_on_prior_highs_only():
    bars = [mk(0, 100, 101, 99, 100), mk(5, 100, 104, 100, 103),   # entry, anchor 104
            mk(10, 105, 120, 104, 118),                           # low 104 > stop 94; anchor->120
            mk(15, 115, 122, 108, 112)]                           # low 108 <= 120-10=110
    r = run(bars, ExitRules(trail_amount=10.0))
    assert r.reason == REASON_TRAIL
    assert r.exit_premium == pytest.approx(110.0)      # min(110, open 115)


# ---------------------------------------------------------------------------
# Liquidity gate + open-at-end
# ---------------------------------------------------------------------------
def test_low_volume_exit_is_rejected_and_position_stays_open():
    tight = ExecutionScenario("LIQ", adverse_slippage_bps=0.0, min_exit_volume=1_000_000.0)
    bars = [mk(0, 100, 101, 99, 100), mk(5, 100, 101, 99, 100),
            mk(10, 120, 125, 119, 124, vol=10.0)]      # target reached but no liquidity
    r = run(bars, ExitRules(target_premium=120.0), scenario=tight)
    assert r.status == OPEN_AT_END and r.reason == REASON_END


def test_holding_to_the_end_marks_open_at_end():
    bars = [mk(0, 100, 101, 99, 100), mk(5, 100, 104, 100, 103), mk(10, 104, 106, 103, 105)]
    r = run(bars, ExitRules(stop_premium=50.0, target_premium=200.0))
    assert r.status == OPEN_AT_END and r.exit_premium is None
    # gross valued against the last observable close
    assert r.gross_pnl == pytest.approx((105.0 - 100.0) * 75 * 1)


# ---------------------------------------------------------------------------
# Scenario ordering (the anti-fragility property)
# ---------------------------------------------------------------------------
def test_pnl_is_monotonic_from_optimistic_to_conservative():
    bars = [mk(0, 100, 101, 99, 100), mk(5, 100, 104, 100, 103), mk(10, 105, 112, 104, 110),
            mk(15, 112, 121, 111, 118)]
    rules = ExitRules(stop_premium=90.0, target_premium=120.0)
    engine = OptionsExecutionEngine(commission=IndiaOptionsCommission())
    a = run(bars, rules, scenario=OPTIMISTIC, engine=engine)
    b = run(bars, rules, scenario=BASELINE, engine=engine)
    c = run(bars, rules, scenario=CONSERVATIVE, engine=engine)
    assert a.net_pnl >= b.net_pnl >= c.net_pnl
    # P2.1.2: the fixture date 2025-01-02 sits in the Oct-2024 sourced era
    # (FA64232), so with brokerage excluded the trade IS strict-verifiable.
    assert a.strict_verifiable is True


def test_a_slab_era_trade_is_costed_but_not_strict_verifiable():
    """Fail-closed: a 2022 trade uses the estimated slab convention."""
    bars = [OptionBar(datetime(2022, 1, 3, 9, 15), 100.0, 101.0, 99.0, 100.0, 1_000_000.0),
            OptionBar(datetime(2022, 1, 3, 9, 20), 100.0, 104.0, 100.0, 103.0, 1_000_000.0),
            OptionBar(datetime(2022, 1, 3, 9, 25), 112.0, 121.0, 111.0, 118.0, 1_000_000.0)]
    engine = OptionsExecutionEngine(commission=IndiaOptionsCommission())
    r = engine.run(bars, 0, ExitRules(target_premium=120.0), lots=1, lot_size=25,
                   scenario=BASELINE)
    assert r.status == FILLED and r.total_costs > 0
    assert r.strict_verifiable is False


def test_run_scenarios_returns_all_three_keyed_by_name():
    bars = [mk(0, 100, 101, 99, 100), mk(5, 100, 104, 100, 103), mk(10, 112, 121, 111, 118)]
    out = ENGINE.run_scenarios(bars, 0, ExitRules(target_premium=120.0),
                               lots=1, lot_size=75, scenarios=(OPTIMISTIC, BASELINE, CONSERVATIVE))
    assert set(out) == {"A_OPTIMISTIC", "B_BASELINE", "C_CONSERVATIVE"}
    assert all(r.reason == REASON_TARGET for r in out.values())


# ---------------------------------------------------------------------------
# Guard rails
# ---------------------------------------------------------------------------
def test_non_positive_lots_or_lot_size_are_refused():
    bars = [mk(0, 100, 101, 99, 100), mk(5, 100, 104, 100, 103)]
    with pytest.raises(ValueError):
        ENGINE.run(bars, 0, ExitRules(), lots=0, lot_size=75, scenario=OPTIMISTIC)
    with pytest.raises(ValueError):
        ENGINE.run(bars, 0, ExitRules(), lots=1, lot_size=0, scenario=OPTIMISTIC)


def test_an_available_bar_must_respect_ohlc_containment():
    with pytest.raises(ValueError):
        mk(0, 100, 90, 110, 100)   # high < low is impossible


def test_no_commission_means_results_are_strict_verifiable():
    bars = [mk(0, 100, 101, 99, 100), mk(5, 100, 104, 100, 103), mk(10, 112, 121, 111, 118)]
    r = run(bars, ExitRules(target_premium=120.0))
    assert r.status == FILLED and r.strict_verifiable is True
