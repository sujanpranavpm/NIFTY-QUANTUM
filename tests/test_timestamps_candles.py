"""T9: timestamp convention and candle aggregation.

The convention was resolved by measurement, not assumption (``logs/m4_terminal_candle.txt``):
a bar stamped ``T`` covers the half-open interval ``[T, T+1)``, and the ``15:30`` bar is the
session's closing print rather than a minute of trade.  These tests pin both halves of that
decision - the interval semantics and the strict rejection of any session that cannot be
given a verified meaning.

No test here reads a downloaded file: every block is built by the ``minute_block`` fixture,
which writes one rupee of price movement and one unit of open interest per minute bucket so
expected OHLC, volume and last-OI values can be written out by hand.
"""

from __future__ import annotations

from datetime import date, time

import pandas as pd
import pytest

from nifty_quant.derivatives import candles as candles_module
from nifty_quant.derivatives.candles import (
    CLOSING_PRINT_INDEX,
    SESSION_MINUTE_BUCKETS,
    aggregate,
    aggregate_contract,
    candle_index,
    candle_label,
    minute_index,
    verdicts_by_block,
)
from nifty_quant.derivatives.timestamps import (
    BAR_MINUTES,
    CLOSE_PRINT_MAX_SHARE,
    CONVENTION,
    OPENING_MIN_SHARE,
    SESSION_CLOSE,
    SESSION_OPEN,
    STRICT_OK,
    STRICT_VERDICTS,
    AMBIGUOUS_NO_TRUSTED_FIRST_BUCKET,
    AMBIGUOUS_OPENING_BUCKET,
    AMBIGUOUS_OUT_OF_SESSION,
    AMBIGUOUS_TERMINAL_BUCKET,
    NO_CLOSING_PRINT,
    bar_interval,
    classify_session,
    clock_label,
    clock_labels,
    contains_trade,
    first_last_bucket,
    in_session,
    label_set,
    minute_volume_profile,
    session_close_stamp,
)
from conftest import minute_labels

IST = "Asia/Kolkata"
#: The verified session: 375 in-session minute buckets 09:15..15:29, then the print.
FULL_SESSION = minute_labels(SESSION_MINUTE_BUCKETS) + ["15:30"]


# ---------------------------------------------------------------------------
# Interval semantics
# ---------------------------------------------------------------------------
def test_the_resolved_convention_is_recorded_where_a_reader_will_find_it():
    assert CONVENTION == "open_labelled"
    assert (SESSION_OPEN, SESSION_CLOSE) == (time(9, 15), time(15, 30))
    assert BAR_MINUTES == 1


def test_a_bar_covers_the_half_open_interval_starting_at_its_own_timestamp():
    start, end = bar_interval("2026-01-05 09:15:00")
    assert (start.strftime("%H:%M"), end.strftime("%H:%M")) == ("09:15", "09:16")
    start, end = bar_interval("2026-01-05 09:15:00", minutes=5)
    assert end.strftime("%H:%M") == "09:20"


@pytest.mark.parametrize("trade,expected", [
    ("2026-01-05 09:15:00", True),      # left edge belongs to the bar
    ("2026-01-05 09:15:59.999000", True),
    ("2026-01-05 09:16:00", False),     # and the next bar owns its own first second
    ("2026-01-05 09:14:59", False),
])
def test_trade_membership_is_half_open_on_the_left(trade, expected):
    assert contains_trade("2026-01-05 09:15:00", trade) is expected


def test_mixed_naive_and_aware_inputs_are_resolved_in_the_session_zone():
    stamp = pd.Timestamp("2026-01-05 09:15:00").tz_localize(IST)
    # 03:45Z is 09:15 IST: the same instant, so it belongs to the bar.
    assert contains_trade(stamp, "2026-01-05 03:45:30+00:00") is True
    assert contains_trade(stamp, "2026-01-05 03:46:00+00:00") is False
    # a naive wall clock is read as IST, not as UTC
    assert contains_trade("2026-01-05 09:15:00", "2026-01-05 03:45:30+00:00") is True


def test_the_interval_is_always_reported_in_indian_time():
    utc = pd.Timestamp("2026-01-05 03:45:00", tz="UTC")
    start, end = bar_interval(utc)
    assert str(start.tz) == IST
    assert (start.strftime("%H:%M"), end.strftime("%H:%M")) == ("09:15", "09:16")


def test_session_close_stamp_is_the_exact_instant_the_market_ends():
    for day in [date(2026, 1, 5), "2026-01-05", pd.Timestamp("2026-01-05 11:30")]:
        stamp = session_close_stamp(day)
        assert stamp.strftime("%H:%M:%S") == "15:30:00"
        assert str(stamp.tz) == IST


@pytest.mark.parametrize("value,expected", [
    ("2026-01-05 09:15:00", "09:15"),
    (pd.Timestamp("2026-01-05 09:15:00", tz=IST), "09:15"),
    (pd.Timestamp("2026-01-05 03:45:00", tz="UTC"), "09:15"),   # same instant, IST clock
    (pd.Timestamp("2026-01-05 15:30:00", tz=IST), "15:30"),
])
def test_clock_label_reads_the_wall_clock_and_never_the_stored_offset(value, expected):
    assert clock_label(value) == expected
    assert clock_label(value, "%H:%M:%S") == expected + ":00"


def test_clock_labels_are_vectorised_and_keep_the_input_rows():
    frame = pd.DataFrame({"timestamp": pd.to_datetime(
        [f"2026-01-05 09:{m:02d}:00" for m in (15, 16, 30)])})
    out = clock_labels(frame["timestamp"])
    assert out.tolist() == ["09:15", "09:16", "09:30"]
    assert list(out.index) == list(frame.index)


@pytest.mark.parametrize("label,inside", [
    ("09:14", False), ("09:15", True), ("12:30", True), ("15:30", True),
    ("15:31", False), ("15:54", False),
])
def test_in_session_spans_the_open_instant_through_the_closing_print(label, inside):
    assert in_session(label) is inside


def test_label_set_is_sorted_and_deduplicated(minute_block):
    block = minute_block(labels=["09:16", "09:15", "09:15"])
    assert label_set(block) == ("09:15", "09:16")


def test_minute_volume_profile_aggregates_the_whole_block(minute_block):
    block = minute_block(labels=["09:15", "09:16", "15:30"],
                         volume_by_label={"09:15": 500.0, "15:30": 4.0})
    profile = minute_volume_profile(block)
    assert list(profile.index) == ["09:15", "09:16", "15:30"]
    # two contracts per bucket: the profile is the block total, which is what separates
    # a closing print from a full minute
    assert profile.tolist() == [1_000.0, 200.0, 8.0]


def test_first_last_bucket_reports_per_contract_ratios(minute_block):
    block = minute_block(labels=minute_labels(25) + ["15:30"],
                         volume_by_label={"09:15": 500.0, "15:30": 4.0})
    out = first_last_bucket(block)
    assert out["n_contracts"] == 2
    assert out["first_bucket_ratio_median"] == 5.0
    assert out["last_bucket_ratio_median"] == 0.04


def test_first_last_bucket_says_nothing_about_a_thin_block(minute_block):
    assert first_last_bucket(minute_block(labels=minute_labels(5))) == {}


# ---------------------------------------------------------------------------
# Session classification: the two-sided test that keeps guessing out of the pipeline
# ---------------------------------------------------------------------------
def ok_block(minute_block):
    """09:15 carries the opening auction (5x a typical minute) and 15:30 is a print."""
    return minute_block(labels=minute_labels(25) + ["15:30"],
                        volume_by_label={"09:15": 500.0, "15:30": 4.0},
                        degenerate_labels=("15:30",))


def test_a_block_matching_the_open_labelled_prediction_is_strictly_usable(minute_block):
    verdict = classify_session(ok_block(minute_block), trading_day="2026-01-05",
                              expiry="2026-01-29")
    assert verdict.verdict == STRICT_OK
    assert verdict.strict_usable is True
    assert verdict.reasons == ()
    assert (verdict.first_label, verdict.last_label, verdict.n_labels) == (
        "09:15", "15:30", 26)
    assert verdict.opening_share == pytest.approx(5.0)
    assert verdict.terminal_share == pytest.approx(0.04)
    assert verdict.stray_labels == ()
    assert STRICT_VERDICTS == (STRICT_OK,)


def test_the_terminal_bucket_degeneracy_is_measured_per_contract(minute_block):
    """83% of real closing prints are single-priced: a point print, not a negotiation."""
    degenerate = classify_session(ok_block(minute_block))
    assert degenerate.terminal_bucket_close_pct == 100.0
    ordinary = classify_session(minute_block(labels=minute_labels(25) + ["15:30"],
                                            volume_by_label={"09:15": 500.0,
                                                             "15:30": 4.0}))
    assert ordinary.terminal_bucket_close_pct == 0.0
    # and a block too thin to have a per-contract "last minute" reports nothing
    thin = classify_session(minute_block(labels=minute_labels(5) + ["15:30"],
                                         volume_by_label={"09:15": 500.0}))
    assert thin.terminal_bucket_close_pct is None


def classify(frame):
    return classify_session(frame, trading_day="2026-01-05", expiry="2026-01-29")


@pytest.mark.parametrize("builder,label", [
    (lambda mb: mb(labels=minute_labels(25) + ["15:30", "15:45"]),
     AMBIGUOUS_OUT_OF_SESSION),
    (lambda mb: mb(labels=["09:14"] + minute_labels(25)), AMBIGUOUS_OUT_OF_SESSION),
    (lambda mb: mb(labels=minute_labels(375)), NO_CLOSING_PRINT),
    (lambda mb: mb(labels=minute_labels(25) + ["15:30"],
                   volume_by_label={"09:15": 10.0, "15:30": 4.0}),
     AMBIGUOUS_OPENING_BUCKET),
    (lambda mb: mb(labels=minute_labels(25) + ["15:30"],
                   volume_by_label={"09:15": 500.0, "15:30": 300.0}),
     AMBIGUOUS_TERMINAL_BUCKET),
    (lambda mb: mb(labels=["09:15", "15:30"]), AMBIGUOUS_NO_TRUSTED_FIRST_BUCKET),
    (lambda mb: mb(labels=minute_labels(25, start_index=1)),
     AMBIGUOUS_NO_TRUSTED_FIRST_BUCKET),
])
def test_every_other_shape_is_rejected_with_its_reason(minute_block, builder, label):
    verdict = classify(builder(minute_block))
    assert verdict.verdict == label
    assert verdict.strict_usable is False
    assert verdict.reasons, "a rejection must explain itself"


def test_a_stray_label_is_reported_rather_than_trimmed(minute_block):
    verdict = classify(minute_block(labels=minute_labels(25) + ["15:30", "15:45", "15:54"]))
    assert verdict.verdict == AMBIGUOUS_OUT_OF_SESSION
    assert verdict.stray_labels == ("15:45", "15:54")
    assert "no convention can be verified" in verdict.reasons[0]


def test_a_thin_block_cannot_establish_a_typical_minute(minute_block):
    verdict = classify(minute_block(labels=["09:15", "15:30"]))
    assert verdict.opening_share is None
    assert "too few interior minutes" in verdict.reasons[0]


def test_a_quiet_open_is_read_as_the_close_labelled_alternative(minute_block):
    verdict = classify(minute_block(labels=minute_labels(25) + ["15:30"],
                                   volume_by_label={"09:15": 10.0, "15:30": 4.0}))
    assert verdict.opening_share == pytest.approx(0.1)
    assert "close-labelled prediction" in verdict.reasons[0]
    # the floor the measurement produced, quoted so a change here is a decision
    assert OPENING_MIN_SHARE == 1.0


def test_a_full_minute_at_the_close_is_not_a_closing_print(minute_block):
    verdict = classify(minute_block(labels=minute_labels(25) + ["15:30"],
                                   volume_by_label={"09:15": 500.0, "15:30": 300.0}))
    assert verdict.terminal_share == pytest.approx(3.0)
    assert "whole minute" in verdict.reasons[0]
    assert CLOSE_PRINT_MAX_SHARE == 0.5


def test_a_block_that_stops_at_15_29_has_no_verified_close(minute_block):
    verdict = classify(minute_block(labels=minute_labels(375)))
    assert verdict.verdict == NO_CLOSING_PRINT
    assert verdict.last_label == "15:29"
    assert verdict.n_labels == SESSION_MINUTE_BUCKETS
    assert "closing print" in verdict.reasons[0]


def test_classification_needs_the_contract_identity_to_be_meaningful(minute_block):
    with pytest.raises(ValueError, match="strike and option_type"):
        classify(minute_block().drop(columns=["strike"]))


# ---------------------------------------------------------------------------
# Coarse grids
# ---------------------------------------------------------------------------
def test_minute_index_counts_buckets_from_the_session_open():
    assert minute_index("09:15") == 0
    assert minute_index("09:16") == 1
    assert minute_index("15:29") == 374
    assert minute_index("15:30") == CLOSING_PRINT_INDEX == 375
    assert minute_index("10:15", session_open="09:15") == 60


@pytest.mark.parametrize("clock,expected", [
    ("09:15", 0), ("09:19", 0), ("09:20", 1), ("09:24", 1), ("09:25", 2),
    ("15:25", 74), ("15:29", 74),
])
def test_the_five_minute_grid_tiles_the_session(clock, expected):
    assert candle_index(clock, 5) == expected


def test_the_closing_print_is_folded_into_the_candle_that_ends_the_session():
    """Otherwise the last traded price of the day is invisible to candle consumers."""
    assert candle_index("15:30", 5) == candle_index("15:25", 5) == 74
    assert candle_index("15:30", 15) == candle_index("15:15", 15) == 24


def test_the_session_tiles_into_the_expected_number_of_candles():
    assert len({candle_index(c, 5) for c in FULL_SESSION}) == 75
    assert len({candle_index(c, 15) for c in FULL_SESSION}) == 25
    assert len({candle_index(c, 3) for c in FULL_SESSION}) == SESSION_MINUTE_BUCKETS // 3


@pytest.mark.parametrize("minutes", [0, 2, 6, 7])
def test_a_grid_that_does_not_tile_the_session_is_refused(minutes):
    with pytest.raises(ValueError, match="do not tile the session evenly"):
        candle_index("09:15", minutes)


@pytest.mark.parametrize("clock", ["09:14", "15:31", "15:45", "00:00"])
def test_a_clock_outside_the_verified_session_is_refused(clock):
    with pytest.raises(ValueError, match="outside the verified session"):
        candle_index(clock, 5)


def test_a_candle_is_labelled_by_its_left_edge():
    assert candle_label(0, 5).strftime("%H:%M") == "09:15"
    assert candle_label(1, 5).strftime("%H:%M") == "09:20"
    assert candle_label(74, 5).strftime("%H:%M") == "15:25"
    assert candle_label(24, 15).strftime("%H:%M") == "15:15"
    assert candle_label(0, 5, session_open="09:15") == candle_label(0, 5)


# ---------------------------------------------------------------------------
# Aggregation of one contract
# ---------------------------------------------------------------------------
def test_a_five_minute_candle_takes_open_high_low_close_from_its_constituents():
    grp = pd.DataFrame({
        "timestamp": pd.to_datetime([f"2026-01-05 09:{m}:00"
                                     for m in ("15", "16", "17", "18", "19")]),
        "open": [100.0, 104.0, 101.0, 106.0, 103.0],
        "high": [102.0, 105.0, 103.0, 107.0, 105.0],
        "low": [99.0, 100.0, 98.0, 101.0, 100.0],
        "close": [104.0, 101.0, 106.0, 103.0, 108.0],
        "volume": [750.0, 150.0, 300.0, 75.0, 225.0],
        "open_interest": [900_000.0, 900_100.0, 900_200.0, 900_300.0, 900_400.0],
    })
    out = aggregate_contract(grp, 5)
    assert len(out) == 1
    row = out.iloc[0]
    assert row["candle_open"].strftime("%H:%M") == "09:15"
    assert row["open"] == 100.0          # first constituent's open
    assert row["high"] == 107.0          # across all constituents
    assert row["low"] == 98.0
    assert row["close"] == 108.0         # last constituent's close
    assert row["volume"] == pytest.approx(1_500.0)
    assert row["open_interest"] == 900_400.0   # the last minute's observation
    assert row["n_minute_buckets"] == 5
    assert row["last_minute_label"] == "09:19"
    assert not bool(row["contains_closing_print"])
    assert bool(row["oi_is_last_minute_observation"])


def test_unsorted_input_is_aggregated_in_clock_order():
    grp = pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-01-05 09:17:00", "2026-01-05 09:15:00",
                                     "2026-01-05 09:16:00"]),
        "open": [1.0, 3.0, 2.0], "high": [1.5, 3.5, 2.5], "low": [0.5, 2.5, 1.5],
        "close": [1.2, 3.2, 2.2], "volume": [10.0, 20.0, 30.0],
        "open_interest": [7.0, 9.0, 8.0],
    })
    row = aggregate_contract(grp, 5).iloc[0]
    assert (row["open"], row["close"], row["open_interest"]) == (3.0, 1.2, 7.0)
    assert row["volume"] == 60.0
    assert row["last_minute_label"] == "09:17"


def test_the_closing_print_becomes_the_final_candle_close():
    grp = pd.DataFrame({
        "timestamp": pd.to_datetime([f"2026-01-05 15:{m}:00"
                                     for m in ("25", "26", "27", "28", "29", "30")]),
        "open": [100.0, 101.0, 102.0, 103.0, 104.0, 105.0],
        "high": [100.0, 101.0, 102.0, 103.0, 104.0, 105.0],
        "low": [100.0, 101.0, 102.0, 103.0, 104.0, 105.0],
        "close": [100.0, 101.0, 102.0, 103.0, 104.0, 105.0],
        "volume": [300.0, 300.0, 300.0, 300.0, 300.0, 75.0],
        "open_interest": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
    })
    out = aggregate_contract(grp, 5)
    assert len(out) == 1
    row = out.iloc[0]
    assert row["candle_open"].strftime("%H:%M") == "15:25"
    assert row["close"] == 105.0                  # the official close, not 15:29's 104
    assert bool(row["contains_closing_print"])
    assert row["last_minute_label"] == "15:30"
    assert row["n_minute_buckets"] == 6
    assert row["volume"] == 1_575.0


def test_empty_intervals_are_not_filled_with_an_invented_candle():
    grp = pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-01-05 09:15:00", "2026-01-05 09:16:00",
                                     "2026-01-05 09:30:00"]),
        "open": [10.0, 11.0, 12.0], "high": [10.0, 11.0, 12.0],
        "low": [10.0, 11.0, 12.0], "close": [10.0, 11.0, 12.0],
        "volume": [75.0, 75.0, 75.0], "open_interest": [1.0, 1.0, 1.0],
    })
    out = aggregate_contract(grp, 5)
    assert out["candle_open"].dt.strftime("%H:%M").tolist() == ["09:15", "09:30"]
    assert out["n_minute_buckets"].tolist() == [2, 1]
    # the 09:20 and 09:25 candles simply do not exist
    assert len(out) == 2


def test_a_missing_open_interest_observation_stays_missing():
    grp = pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-01-05 09:15:00", "2026-01-05 09:16:00"]),
        "open": [1.0, 2.0], "high": [1.0, 2.0], "low": [1.0, 2.0],
        "close": [1.0, 2.0], "volume": [75.0, 75.0],
        "open_interest": [500.0, float("nan")],
    })
    assert pd.isna(aggregate_contract(grp, 5).iloc[0]["open_interest"])


# ---------------------------------------------------------------------------
# Block-level aggregation, with strict validation in charge
# ---------------------------------------------------------------------------
def test_a_verified_block_aggregates_and_conserves_volume(minute_block):
    block = ok_block(minute_block)
    result = aggregate(block, minutes=5)
    assert result.n_rejected == 0
    # buckets 09:15..09:39 give candles 0..4, the 15:30 print gives candle 74
    assert len(result.candles) == 6 * 2          # two contracts
    assert result.candles["volume"].sum() == pytest.approx(block["volume"].sum())
    assert result.candles["session_verdict"].unique().tolist() == [STRICT_OK]
    per_contract = result.candles.groupby(["strike", "option_type"]).size()
    assert (per_contract == 6).all()
    assert per_contract.max() <= 75


def test_the_final_candle_of_a_verified_day_closes_on_the_closing_print(minute_block):
    result = aggregate(ok_block(minute_block), minutes=5)
    last = result.candles.sort_values("candle_open").groupby(
        ["strike", "option_type"]).tail(1)
    assert len(last) == 2
    assert (last["candle_open"].dt.strftime("%H:%M") == "15:25").all()
    assert (last["contains_closing_print"]).all()
    # price of bucket 25 (09:15 + 24 minutes, then the 15:30 print) = 100 + 25
    assert (last["close"] == 125.0).all()
    assert (last["open_interest"] == 1_025.0).all()


def test_five_and_fifteen_minute_grids_agree_on_the_day_total(minute_block):
    block = ok_block(minute_block)
    fine = aggregate(block, minutes=5).candles
    coarse = aggregate(block, minutes=15).candles
    assert fine["volume"].sum() == pytest.approx(coarse["volume"].sum())
    assert len(coarse) < len(fine)


@pytest.mark.parametrize("builder,label", [
    (lambda mb: mb(labels=minute_labels(25) + ["15:30", "15:45"]),
     AMBIGUOUS_OUT_OF_SESSION),
    (lambda mb: mb(labels=minute_labels(25) + ["15:30"],
                   volume_by_label={"09:15": 10.0, "15:30": 4.0}),
     AMBIGUOUS_OPENING_BUCKET),
    (lambda mb: mb(labels=minute_labels(375)), NO_CLOSING_PRINT),
])
def test_an_unverified_session_is_rejected_and_the_reason_is_kept(minute_block, builder,
                                                                 label):
    result = aggregate(builder(minute_block), minutes=5)
    assert result.candles.empty, "a rejected session must not produce candles"
    assert result.n_rejected == 1
    row = result.rejected.iloc[0]
    assert row["verdict"] == label
    assert row["trading_day"] == "2026-01-05" and row["expiry"] == "2026-01-29"
    assert row["reason"]


def test_strict_false_aggregates_a_shape_failed_block_but_not_an_impossible_clock(
        minute_block):
    quiet_open = minute_block(labels=minute_labels(25) + ["15:30"],
                              volume_by_label={"09:15": 10.0, "15:30": 4.0})
    assert len(aggregate(quiet_open, minutes=5, strict=False).candles) == 12
    # bypassing validation does not make a label outside the session aggregable:
    # there is no honest candle for it, and silence would be a guess.
    stray = minute_block(labels=minute_labels(25) + ["15:30", "15:45"])
    with pytest.raises(ValueError, match="outside the verified session"):
        aggregate(stray, minutes=5, strict=False)


def test_aggregation_is_refused_until_the_convention_is_resolved(minute_block,
                                                                monkeypatch):
    monkeypatch.setattr(candles_module, "CONVENTION", "unresolved")
    with pytest.raises(RuntimeError, match="requires a resolved timestamp convention"):
        aggregate(ok_block(minute_block), minutes=5)


def test_aggregate_reports_which_columns_it_needed(minute_block):
    with pytest.raises(ValueError, match="aggregate needs"):
        aggregate(minute_block().drop(columns=["trading_day"]), minutes=5)


def test_each_day_and_expiry_is_judged_on_its_own_labels(minute_block):
    good = ok_block(minute_block)
    bad = minute_block(labels=minute_labels(25) + ["15:45"])
    combined = pd.concat([good, bad.assign(trading_day="2026-01-06")], ignore_index=True)
    table = verdicts_by_block(combined)
    assert len(table) == 2
    assert set(table["verdict"]) == {STRICT_OK, AMBIGUOUS_OUT_OF_SESSION}
    result = aggregate(combined, minutes=5)
    assert result.n_rejected == 1
    assert set(result.rejected["trading_day"]) == {"2026-01-06"}
    assert not result.candles.empty
