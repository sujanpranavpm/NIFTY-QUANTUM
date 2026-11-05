"""P2 stage 4 - 1m / 5m / 15m candle generation and integrity.

The library functions the generation script is built from are tested against
hand-built, deterministic blocks (the ``minute_block`` fixture writes one rupee of price
and one unit of OI per bucket, so every expected value is written out literally).  A
final group of tests reads the *real* datasets the script writes to ``data/derived``
and is skipped when that directory has not been generated, so the suite still passes on
a fresh clone while the acceptance run exercises the persisted files.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from nifty_quant.derivatives.candles import (
    SESSION_MINUTE_BUCKETS, aggregate, aggregate_from_1m, closing_print_candles,
    minute_pass_through, ohlc_containment_failures, volume_conservation_delta)
from nifty_quant.derivatives.timestamps import STRICT_OK
from conftest import minute_labels

DERIVED = Path("data/derived/candles")


def full_session(minute_block):
    """A verified whole session: 09:15 open auction, 15:30 print, 375 in-session buckets."""
    return minute_block(labels=minute_labels(SESSION_MINUTE_BUCKETS) + ["15:30"],
                        volume_by_label={"09:15": 500.0, "15:30": 4.0},
                        degenerate_labels=("15:30",))


def quiet_block(minute_block):
    """09:15 carries only a typical minute (no opening auction) -> rejected."""
    return minute_block(labels=minute_labels(25) + ["15:30"],
                        volume_by_label={"09:15": 10.0, "15:30": 4.0},
                        degenerate_labels=("15:30",))


# ---------------------------------------------------------------------------
# 1-minute canonical pass-through
# ---------------------------------------------------------------------------
def test_pass_through_keeps_raw_values_verbatim_and_annotates(minute_block):
    block = full_session(minute_block)
    one = minute_pass_through(block).candles
    # every raw OHLCV/OI column is byte-for-byte the input
    for col in ("open", "high", "low", "close", "volume", "open_interest"):
        assert one[col].tolist() == block[col].tolist()
    assert len(one) == len(block)
    assert set(["minute_label", "is_closing_print", "session_verdict"]) <= set(one.columns)
    assert one["session_verdict"].unique().tolist() == [STRICT_OK]


def test_pass_through_keeps_the_closing_print_as_its_own_raw_row(minute_block):
    one = minute_pass_through(full_session(minute_block)).candles
    prints = one[one["is_closing_print"]]
    # two contracts x one 15:30 print each, still present (never folded away at 1m)
    assert len(prints) == 2
    assert (prints["minute_label"] == "15:30").all()


def test_pass_through_rejects_an_untrusted_block_and_reports_the_reason(minute_block):
    result = minute_pass_through(quiet_block(minute_block))
    assert result.candles.empty
    assert result.n_rejected == 1
    assert result.rejected.iloc[0]["reason"]


def test_pass_through_mixes_good_and_bad_days_judged_independently(minute_block):
    good = full_session(minute_block)
    bad = quiet_block(minute_block).assign(trading_day="2026-01-06")
    result = minute_pass_through(pd.concat([good, bad], ignore_index=True))
    assert result.n_rejected == 1
    assert set(result.rejected["trading_day"]) == {"2026-01-06"}
    assert result.candles["trading_day"].unique().tolist() == ["2026-01-05"]


# ---------------------------------------------------------------------------
# Coarse aggregation integrity
# ---------------------------------------------------------------------------
def test_five_minute_grid_of_a_full_session_has_seventy_five_candles(minute_block):
    one = minute_pass_through(full_session(minute_block)).candles
    five = aggregate(one, minutes=5).candles
    per = five.groupby(["strike", "option_type"]).size()
    assert per.tolist() == [75, 75]
    assert five["candle_open"].dt.strftime("%H:%M").max() == "15:25"


def test_fifteen_minute_grid_has_twenty_five_candles_and_conserves_volume(minute_block):
    one = minute_pass_through(full_session(minute_block)).candles
    fifteen = aggregate(one, minutes=15).candles
    assert fifteen.groupby(["strike", "option_type"]).size().tolist() == [25, 25]
    assert volume_conservation_delta(one, fifteen) == pytest.approx(0.0)
    assert ohlc_containment_failures(fifteen) == 0


def test_closing_print_is_folded_into_the_terminal_candle_and_preserved(minute_block):
    one = minute_pass_through(full_session(minute_block)).candles
    five = aggregate(one, minutes=5).candles
    printed = five[five["contains_closing_print"].astype(bool)]
    assert len(printed) == 2                       # one per contract
    assert (printed["candle_open"].dt.strftime("%H:%M") == "15:25").all()
    # the raw 15:30 close is preserved next to the folded close and equals it
    assert printed["close"].tolist() == printed["closing_print"].tolist()
    assert closing_print_candles(five) == 2


def test_only_the_terminal_candle_carries_a_closing_print(minute_block):
    one = minute_pass_through(full_session(minute_block)).candles
    five = aggregate(one, minutes=5).candles
    non_terminal = five[~five["contains_closing_print"].astype(bool)]
    assert non_terminal["closing_print"].isna().all()


def test_volume_conservation_detects_a_dropped_candle(minute_block):
    one = minute_pass_through(full_session(minute_block)).candles
    five = aggregate(one, minutes=5).candles
    assert volume_conservation_delta(one, five) == pytest.approx(0.0)
    assert volume_conservation_delta(one, five.drop(index=five.index[0])) > 0


def test_ohlc_containment_detects_an_invented_candle():
    good = pd.DataFrame([{"open": 10.0, "high": 12.0, "low": 9.0, "close": 11.0}])
    assert ohlc_containment_failures(good) == 0
    bad = pd.DataFrame([{"open": 10.0, "high": 8.0, "low": 9.0, "close": 11.0}])  # high<open
    assert ohlc_containment_failures(bad) == 1


def test_missing_intervals_are_never_filled(minute_block):
    # a block that trades 09:15-09:19 then jumps to the print leaves candles 1..73 absent
    block = minute_block(labels=minute_labels(5) + ["15:30"],
                         volume_by_label={"09:15": 500.0, "15:30": 4.0},
                         degenerate_labels=("15:30",))
    one = minute_pass_through(block).candles
    five = aggregate(one, minutes=5).candles
    labels = sorted(five["candle_open"].dt.strftime("%H:%M").unique())
    assert labels == ["09:15", "15:25"]            # exactly the two occupied slots
    assert len(five) == 4                           # two contracts x two candles


def test_empty_oi_stays_empty_after_aggregation(minute_block):
    block = full_session(minute_block)
    block = block.assign(open_interest=lambda d: d["open_interest"].astype("float64"))
    block.loc[block["close"] == block["close"].max(), "open_interest"] = float("nan")
    one = minute_pass_through(block).candles
    five = aggregate(one, minutes=5).candles
    assert five["open_interest"].isna().any()


def test_aggregation_is_deterministic(minute_block):
    one = minute_pass_through(full_session(minute_block)).candles
    a = aggregate(one, minutes=5).candles
    b = aggregate(one, minutes=5).candles
    pd.testing.assert_frame_equal(a, b)


# ---------------------------------------------------------------------------
# The vectorised generator path must equal the tested aggregate() exactly
# ---------------------------------------------------------------------------
COMPARE_COLUMNS = ["trading_day", "symbol", "expiry", "strike", "option_type",
                   "candle_open", "open", "high", "low", "close", "volume",
                   "open_interest", "n_minute_buckets", "last_minute_label",
                   "contains_closing_print", "closing_print", "closing_print_volume"]


def _canonical(frame):
    cols = [c for c in COMPARE_COLUMNS if c in frame.columns]
    out = frame[cols].sort_values(
        ["trading_day", "symbol", "expiry", "strike", "option_type",
         "candle_open"]).reset_index(drop=True)
    return out


@pytest.mark.parametrize("minutes", [5, 15])
def test_fast_path_matches_aggregate_exactly(minute_block, minutes):
    one = minute_pass_through(full_session(minute_block)).candles
    slow = aggregate(one, minutes=minutes).candles
    fast = aggregate_from_1m(one, minutes).candles
    pd.testing.assert_frame_equal(_canonical(slow), _canonical(fast), check_dtype=False)


def test_fast_path_folds_the_closing_print_and_preserves_its_raw_value(minute_block):
    one = minute_pass_through(full_session(minute_block)).candles
    fast = aggregate_from_1m(one, 5).candles
    printed = fast[fast["contains_closing_print"].astype(bool)]
    assert len(printed) == 2
    assert (printed["candle_open"].dt.strftime("%H:%M") == "15:25").all()
    assert printed["close"].tolist() == printed["closing_print"].tolist()


def test_fast_path_keeps_a_missing_oi_observation_missing(minute_block):
    block = full_session(minute_block)
    block = block.assign(open_interest=lambda d: d["open_interest"].astype("float64"))
    last = block["timestamp"].idxmax()
    block.loc[last, "open_interest"] = float("nan")
    one = minute_pass_through(block).candles
    fast = aggregate_from_1m(one, 5).candles
    assert fast["open_interest"].isna().any()


def test_fast_path_on_an_empty_frame_returns_empty():
    empty = pd.DataFrame(columns=["timestamp", "trading_day", "symbol", "expiry",
                                  "strike", "option_type", "open", "high", "low",
                                  "close", "volume", "open_interest"])
    result = aggregate_from_1m(empty, 5)
    assert result.candles.empty and result.n_rejected == 0


# ---------------------------------------------------------------------------
# Real persisted datasets (skipped until scripts/_p2_generate_candles.py has run)
# ---------------------------------------------------------------------------
pytestmark_disk = pytest.mark.skipif(
    not DERIVED.exists(), reason="run scripts/_p2_generate_candles.py first")


@pytest.fixture(scope="module")
def derived_files():
    if not DERIVED.exists():
        pytest.skip("data/derived/candles not generated")
    return sorted(DERIVED.glob("*.parquet"))


@pytestmark_disk
def test_generated_files_exist_for_all_three_resolutions(derived_files):
    stems = {p.name.split("__")[0] for p in derived_files}
    assert {"1m", "5m", "15m"} <= stems
    expiries = {p.name.split("NIFTY__")[1].replace(".parquet", "") for p in derived_files}
    for exp in expiries:
        for res in ("1m", "5m", "15m"):
            assert (DERIVED / f"{res}__NIFTY__{exp}.parquet").exists()


@pytestmark_disk
def test_coarse_grids_conserve_volume_against_the_one_minute_grid(derived_files):
    expiries = {p.name.split("NIFTY__")[1].replace(".parquet", "") for p in derived_files}
    for exp in sorted(expiries):
        one = pd.read_parquet(DERIVED / f"1m__NIFTY__{exp}.parquet")
        for res in ("5m", "15m"):
            grid = pd.read_parquet(DERIVED / f"{res}__NIFTY__{exp}.parquet")
            assert volume_conservation_delta(one, grid) < 0.5
            assert ohlc_containment_failures(grid) == 0
