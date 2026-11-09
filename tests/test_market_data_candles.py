"""Live market data: local candle aggregation, cumulative volume and gap reporting.

Every test states both halves of the contract: the values a candle must contain, and the
values it must **not** invent (a missing minute, a volume nobody reported).
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from nifty_quant.market_data import _test_support as T
from nifty_quant.market_data.candle_builder import (
    CANDLE_COLUMNS,
    CandleAggregator,
    bucket_start,
    candle_quality_report,
    expected_buckets,
    resample,
)
from nifty_quant.market_data.normalizer import CLEAN, DUPLICATE, Tick

IST = ZoneInfo("Asia/Kolkata")
DAY = datetime(2030, 1, 3)

KEY = "NSE_FO|25000CE"
OTHER = "NSE_FO|25050CE"


def moment(hour: int, minute: int, second: int = 0) -> datetime:
    return DAY.replace(hour=hour, minute=minute, second=second, tzinfo=IST)


OPEN = moment(9, 15)


def tick(ltp: float = 100.0, when: datetime | None = None, *, volume: int | None = None,
         oi: float | None = None, key: str = KEY, quality: str = CLEAN) -> Tick:
    when = when or OPEN
    return Tick(
        instrument_key=key, kind="option", mode="full",
        exchange_ts_ms=T.ms(when), local_ts_ms=T.ms(when),
        ltp=ltp, ltq=75, close_price=99.0, volume=volume, open_interest=oi,
        trading_symbol="NIFTY 03 JAN 30 25000 CE", underlying="NIFTY",
        expiry="2030-01-03", strike=25000, option_type="CE",
        source_provider="fake", quality=quality,
    )


def collect(ticks, *, minutes: int = 1, flush_at: datetime | None = None) -> pd.DataFrame:
    aggregator = CandleAggregator(minutes)
    candles = []
    for item in ticks:
        candles.extend(aggregator.add(item))
    if flush_at is not None:
        candles.extend(aggregator.flush(flush_at))
    candles.extend(aggregator.finalize())
    return pd.DataFrame([c.to_record() for c in candles], columns=CANDLE_COLUMNS)


# ---------------------------------------------------------------------------
# Bucket labelling
# ---------------------------------------------------------------------------
def test_bucket_is_labelled_by_its_left_edge():
    assert bucket_start(moment(9, 15, 30)) == moment(9, 15)
    assert bucket_start(moment(9, 15, 59)) == moment(9, 15)
    assert bucket_start(moment(9, 16, 0)) == moment(9, 16)


def test_closing_print_is_folded_into_the_last_session_bucket():
    assert bucket_start(moment(15, 30, 0)) == moment(15, 29)


def test_timestamps_outside_the_session_have_no_bucket():
    assert bucket_start(moment(9, 0)) is None
    assert bucket_start(moment(16, 0)) is None


def test_five_minute_buckets_tile_the_session():
    assert bucket_start(moment(9, 17), 5) == moment(9, 15)
    assert bucket_start(moment(9, 19, 59), 5) == moment(9, 15)
    assert bucket_start(moment(9, 20), 5) == moment(9, 20)
    assert expected_buckets(5) == 75
    assert expected_buckets(15) == 25


def test_a_timeframe_that_does_not_tile_the_session_is_refused():
    with pytest.raises(ValueError):
        bucket_start(moment(9, 15), 7)


# ---------------------------------------------------------------------------
# OHLC from prints
# ---------------------------------------------------------------------------
def test_one_minute_ohlc_comes_from_the_prints_in_that_minute():
    prices = [(100.0, moment(9, 15, 5)), (102.5, moment(9, 15, 20)),
              (99.5, moment(9, 15, 40)), (101.0, moment(9, 15, 55))]
    frame = collect([tick(p, w) for p, w in prices])
    assert len(frame) == 1
    row = frame.iloc[0]
    assert row["open"] == pytest.approx(100.0)
    assert row["high"] == pytest.approx(102.5)
    assert row["low"] == pytest.approx(99.5)
    assert row["close"] == pytest.approx(101.0)
    assert row["n_ticks"] == 4
    assert row["timeframe_minutes"] == 1


def test_a_new_minute_closes_the_previous_candle_as_complete():
    frame = collect([tick(100.0, moment(9, 15, 10)), tick(101.0, moment(9, 16, 10))])
    first = frame[frame["candle_open"] == OPEN].iloc[0]
    second = frame[frame["candle_open"] == moment(9, 16)].iloc[0]
    assert bool(first["is_complete"]) is True
    assert bool(second["is_complete"]) is False   # still open when finalize() ran


def test_minutes_without_ticks_produce_no_candle():
    frame = collect([tick(100.0, moment(9, 15, 10)), tick(101.0, moment(9, 20, 10))])
    labels = [stamp.strftime("%H:%M") for stamp in frame["candle_open"]]
    assert labels[0] == "09:15"
    assert "09:16" not in labels and "09:19" not in labels


def test_duplicate_and_flagged_ticks_never_move_a_candle():
    frame = collect([
        tick(100.0, moment(9, 15, 10)),
        tick(150.0, moment(9, 15, 20), quality=DUPLICATE),
    ])
    row = frame.iloc[0]
    assert row["high"] == pytest.approx(100.0)
    assert row["n_ticks"] == 1


def test_open_interest_is_the_last_observation_not_a_sum():
    frame = collect([
        tick(100.0, moment(9, 15, 10), oi=5000.0),
        tick(101.0, moment(9, 15, 40), oi=5100.0),
    ])
    assert frame.iloc[0]["open_interest"] == pytest.approx(5100.0)


# ---------------------------------------------------------------------------
# Cumulative volume
# ---------------------------------------------------------------------------
def test_volume_is_differenced_not_summed():
    # vtt 1000 -> 1050 -> 1100: the minute traded 100, not 3150.
    frame = collect([
        tick(100.0, moment(9, 15, 5), volume=1000),
        tick(100.0, moment(9, 15, 20), volume=1050),
        tick(100.0, moment(9, 15, 45), volume=1100),
    ])
    row = frame.iloc[0]
    assert row["volume"] == pytest.approx(100.0)
    assert row["volume_basis"] == "cumulative_delta"


def test_the_first_observation_of_a_session_does_not_claim_the_days_volume():
    frame = collect([tick(100.0, moment(9, 15, 5), volume=987654),
                     tick(100.0, moment(9, 15, 30), volume=987700)])
    row = frame.iloc[0]
    assert row["volume"] == pytest.approx(46.0)
    assert bool(row["volume_complete"]) is False   # volume before we connected is unknown


def test_a_volume_counter_reset_is_a_new_day_not_negative_volume():
    aggregator = CandleAggregator(1)
    aggregator.add(tick(100.0, moment(9, 15, 5), volume=1000))
    aggregator.add(tick(100.0, moment(9, 15, 30), volume=50))     # rolled over
    candles = aggregator.finalize()
    row = candles[0].to_record()
    assert row["volume"] == pytest.approx(50.0)
    assert "volume_counter_reset" in row["quality_flags"]


def test_an_instrument_without_volume_reports_unavailable_not_zero():
    frame = collect([tick(25010.0, moment(9, 15, 5), volume=None)])
    row = frame.iloc[0]
    assert pd.isna(row["volume"])
    assert row["volume_basis"] == "unavailable"
    assert bool(row["volume_complete"]) is False


def test_volume_is_tracked_per_instrument():
    aggregator = CandleAggregator(1)
    aggregator.add(tick(100.0, moment(9, 15, 5), volume=1000, key=KEY))
    aggregator.add(tick(50.0, moment(9, 15, 6), volume=8000, key=OTHER))
    aggregator.add(tick(100.0, moment(9, 15, 30), volume=1100, key=KEY))
    aggregator.add(tick(50.0, moment(9, 15, 31), volume=8100, key=OTHER))
    rows = {c.instrument_key: c for c in aggregator.finalize()}
    assert rows[KEY].volume == pytest.approx(100.0)
    assert rows[OTHER].volume == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Coarser timeframes
# ---------------------------------------------------------------------------
def test_five_minute_candle_takes_first_open_last_close_and_summed_volume():
    ticks = [tick(100.0 + i, moment(9, 15 + i, 10), volume=1000 + 10 * (i + 1))
             for i in range(5)]
    one = collect(ticks)
    assert len(one) == 5
    coarse = resample(one, 5)
    assert len(coarse) == 1
    row = coarse.iloc[0]
    assert row["open"] == pytest.approx(100.0)
    assert row["close"] == pytest.approx(104.0)
    assert row["high"] == pytest.approx(104.0)
    assert row["low"] == pytest.approx(100.0)
    assert row["volume"] == pytest.approx(one["volume"].sum())


def test_fifteen_minute_candle_covers_fifteen_minutes():
    ticks = [tick(100.0 + i, moment(9, 15 + i, 10)) for i in range(15)]
    coarse = resample(collect(ticks), 15)
    assert len(coarse) == 1
    assert coarse.iloc[0]["candle_open"] == OPEN
    assert coarse.iloc[0]["candle_close"] == moment(9, 30)


def test_resampled_volume_is_conserved():
    ticks = [tick(100.0 + (i % 3), moment(9, 15 + i, 10), volume=1000 + 5 * (i + 1))
             for i in range(20)]
    one = collect(ticks)
    for minutes in (5, 15):
        coarse = resample(one, minutes)
        assert float(coarse["volume"].sum()) == pytest.approx(float(one["volume"].sum()))


def test_resample_keeps_instruments_separate():
    ticks = [tick(100.0, moment(9, 15, 10), key=KEY),
             tick(50.0, moment(9, 16, 10), key=OTHER)]
    coarse = resample(collect(ticks), 5)
    assert set(coarse["instrument_key"]) == {KEY, OTHER}


def test_resample_of_one_minute_is_a_passthrough():
    one = collect([tick(100.0, moment(9, 15, 10))])
    assert len(resample(one, 1)) == len(one)


# ---------------------------------------------------------------------------
# Gaps and completeness
# ---------------------------------------------------------------------------
def test_gaps_count_missing_buckets_within_the_observed_span():
    aggregator = CandleAggregator(1)
    for minute in (15, 18, 19):
        aggregator.add(tick(100.0, moment(9, minute, 10)))
    gaps = aggregator.gaps()
    assert gaps[KEY]["missing_buckets"] == 2          # 09:16 and 09:17
    assert aggregator.open_buckets()[KEY] == moment(9, 19)


def test_flush_closes_an_elapsed_bucket_as_complete():
    aggregator = CandleAggregator(1)
    aggregator.add(tick(100.0, moment(9, 15, 10)))
    closed = aggregator.flush(moment(9, 16, 1))
    assert len(closed) == 1
    assert closed[0].is_complete is True


def test_flush_leaves_the_open_bucket_alone():
    aggregator = CandleAggregator(1)
    aggregator.add(tick(100.0, moment(9, 15, 10)))
    assert aggregator.flush(moment(9, 15, 30)) == []


def test_candle_quality_report_counts_incomplete_and_partial_volume():
    frame = collect([tick(100.0, moment(9, 15, 10), volume=1000),
                     tick(100.0, moment(9, 16, 10), volume=1010)])
    report = candle_quality_report(frame)
    assert report["candles"] == 2
    assert report["incomplete"] == 1
    assert report["volume_partial"] == 1
    assert report["instruments"] == 1


def test_candles_are_labelled_as_locally_aggregated():
    frame = collect([tick(100.0, moment(9, 15, 10))])
    assert set(frame["candle_origin"]) == {"locally_aggregated"}
