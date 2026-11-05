"""P2 stage 7 - missing-data behaviour across the whole stack (directive Part 9).

The standing rules are that a gap is never silently handled: a suspect session is refused
rather than aggregated, a missing minute reduces its bucket instead of being filled, a
missing open-interest value stays missing (never zeroed), and an unobservable interval can
never yield a fabricated fill.  These tests pin exactly those behaviours in the data layer
and in the options execution engine.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pytest

from nifty_quant.config import OPTIMISTIC
from nifty_quant.derivatives.candles import (
    SESSION_MINUTE_BUCKETS,
    aggregate,
    aggregate_from_1m,
    minute_pass_through,
    volume_conservation_delta,
)
from nifty_quant.engine.options_execution import (
    REASON_TARGET,
    OPEN_AT_END,
    ExitRules,
    OptionBar,
    OptionsExecutionEngine,
)
from conftest import minute_labels


def full_session(minute_block):
    """A verified whole session: 09:15 open auction, 15:30 print, 375 in-session buckets."""
    return minute_block(labels=minute_labels(SESSION_MINUTE_BUCKETS) + ["15:30"],
                        volume_by_label={"09:15": 500.0, "15:30": 4.0},
                        degenerate_labels=("15:30",))


def untrusted_block(minute_block):
    """09:15 carries only an ordinary minute (no opening auction) -> verdict is not OK."""
    return minute_block(labels=minute_labels(25) + ["15:30"],
                        volume_by_label={"09:15": 10.0, "15:30": 4.0},
                        degenerate_labels=("15:30",))


# ---------------------------------------------------------------------------
# Data layer: sessions, minutes and open interest
# ---------------------------------------------------------------------------
def test_an_untrusted_session_is_rejected_rather_than_aggregated(minute_block):
    result = aggregate(untrusted_block(minute_block), minutes=5)
    assert result.candles.empty
    assert result.n_rejected == 1
    assert result.rejected.iloc[0]["reason"]          # a reason is recorded, never a shrug


def test_a_missing_minute_reduces_its_bucket_rather_than_being_filled(minute_block):
    one = minute_pass_through(full_session(minute_block)).candles
    five_full = aggregate_from_1m(one, 5).candles

    one_missing = one[one["minute_label"] != "10:00"]
    five_missing = aggregate_from_1m(one_missing, 5).candles

    # volume is conserved in both builds (no phantom volume is inserted)
    assert volume_conservation_delta(one, five_full) == pytest.approx(0.0)
    assert volume_conservation_delta(one_missing, five_missing) == pytest.approx(0.0)
    # the gap lowered the total; it did not add or invent a candle
    assert five_missing["volume"].sum() < five_full["volume"].sum()
    assert len(five_missing) == len(five_full)


def test_missing_open_interest_is_preserved_not_zero_filled(minute_block):
    one = minute_pass_through(full_session(minute_block)).candles.copy()
    terminal_minute = one["minute_label"].astype(str) == "10:04"   # tail of the 10:00 bucket
    one.loc[terminal_minute, "open_interest"] = np.nan

    assert one["open_interest"].isna().sum() == int(terminal_minute.sum())  # NaN survives

    five = aggregate_from_1m(one, 5).candles
    bucket = five[five["candle_open"] == "10:00"]
    assert bucket["open_interest"].isna().all()          # last-OI is NaN, never coerced to 0


# ---------------------------------------------------------------------------
# Execution layer: a gap cannot produce a fill
# ---------------------------------------------------------------------------
def _bar(minutes, o, h, l, c, vol=1_000_000.0, *, available=True):
    stamp = datetime(2025, 1, 2, 9, 15) + timedelta(minutes=minutes)
    if not available:
        return OptionBar(stamp, float("nan"), float("nan"), float("nan"), float("nan"),
                         0.0, available=False)
    return OptionBar(stamp, o, h, l, c, vol, available=True)


ENGINE = OptionsExecutionEngine()


def test_a_gap_cannot_produce_a_fabricated_fill():
    bars = [_bar(0, 100, 101, 99, 100), _bar(5, 100, 104, 100, 103),
            _bar(10, 0, 0, 0, 0, available=False),        # unobservable interval
            _bar(15, 105, 110, 104, 108)]                  # neither stop nor target reached
    r = ENGINE.run(bars, 0, ExitRules(stop_premium=90.0, target_premium=120.0),
                   lots=1, lot_size=75, scenario=OPTIMISTIC)
    assert r.status == OPEN_AT_END and r.exit_premium is None


def test_execution_resumes_on_the_next_observable_bar_after_a_gap():
    bars = [_bar(0, 100, 101, 99, 100), _bar(5, 100, 104, 100, 103),
            _bar(10, 0, 0, 0, 0, available=False),        # the gap is skipped, not traded
            _bar(15, 115, 121, 114, 120)]                  # target reached here instead
    r = ENGINE.run(bars, 0, ExitRules(target_premium=120.0),
                   lots=1, lot_size=75, scenario=OPTIMISTIC)
    assert r.reason == REASON_TARGET
    assert r.exit_premium == pytest.approx(120.0)
    assert r.exit_timestamp == bars[3].timestamp          # the observable bar, never the gap
