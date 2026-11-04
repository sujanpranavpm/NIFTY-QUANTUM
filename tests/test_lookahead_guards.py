"""T9: look-ahead prevention and the no-manufacture rule for open interest.

Two separate promises are pinned here.

*Official EOD figures must not leak backwards.* The official NSE F&O bhavcopy is
published after the session ends, so joining ``OpnIntrst`` onto a bar is only ever legal
at or after the 15:30 closing print. ``attach_official_eod`` enforces that by construction
and ``assert_no_eod_leakage`` re-checks it on any frame, including one a caller tampered
with.

*Intraday open interest is never manufactured.* 35.37% of the archive's bars carry a
provider OI of zero (measured on 262 official dates, ``logs/m3_oi_norm.txt``). Those are
missing observations, not positions closing to nothing, and no join, conversion or fill may
turn them into a number.
"""

from __future__ import annotations

from datetime import date, time

import numpy as np
import pandas as pd
import pytest

from conftest import DEFAULT_DAY, DEFAULT_EXPIRY
from nifty_quant.derivatives.reference import LotRegime, LotTable
from nifty_quant.derivatives.units import (
    EOD_JOIN_KEYS,
    NSE_SESSION_CLOSE,
    LookAheadError,
    UNIT_PROVIDER_OI_LATE,
    assert_no_eod_leakage,
    attach_official_eod,
    normalise_units,
)

DAY = DEFAULT_DAY
#: The official end-of-day figure for the test contract, in share equivalents.
OFFICIAL_OI = 1_234_500.0
FLAT_65 = (LotRegime(65, date(2000, 1, 1), date(2199, 12, 31), "test regime"),)


def bars_frame(minute_block, labels=("09:15", "15:29", "15:30"), **kwargs) -> pd.DataFrame:
    """A single-contract day whose provider OI is 900 share-equivalents everywhere."""
    return minute_block(labels=list(labels), contracts=[("CE", 24000)],
                        oi_base=900.0, **kwargs)


def eod(official_eod, expiry=DEFAULT_EXPIRY, option_type="CE", strike=24000,
        value=OFFICIAL_OI) -> pd.DataFrame:
    return official_eod([(DAY, expiry, strike, option_type, value)])


# ---------------------------------------------------------------------------
# The leakage gate
# ---------------------------------------------------------------------------
def test_official_eod_is_invisible_on_every_bar_before_the_close(minute_block,
                                                                official_eod):
    out = attach_official_eod(bars_frame(minute_block), eod(official_eod))
    labels = out["timestamp"].dt.strftime("%H:%M").tolist()
    values = out["oi_official_eod"].tolist()
    assert labels == ["09:15", "15:29", "15:30"]
    assert np.isnan(values[0]) and np.isnan(values[1])
    assert values[2] == OFFICIAL_OI


def test_the_gate_is_on_the_wall_clock_and_not_on_the_stored_offset(minute_block,
                                                                   official_eod):
    """15:30 IST is 10:00 UTC: converting first would move the close and break the guard."""
    frame = bars_frame(minute_block)
    frame["timestamp"] = frame["timestamp"].dt.tz_convert("UTC")
    out = attach_official_eod(frame, eod(official_eod))
    assert out["oi_official_eod"].isna().sum() == 2
    assert out["oi_official_eod"].dropna().tolist() == [OFFICIAL_OI]
    assert_no_eod_leakage(out)


def test_the_join_keeps_every_bar_exactly_once(minute_block, official_eod):
    frame = bars_frame(minute_block)
    frame = pd.concat([frame, frame.assign(strike=24050)], ignore_index=True)
    out = attach_official_eod(frame, eod(official_eod))
    assert len(out) == len(frame) == 6
    pd.testing.assert_index_equal(out.index, frame.index)
    # the 24050 contract has no official row: it gets NaN, not the neighbouring figure
    assert out.loc[out["strike"] == 24050, "oi_official_eod"].isna().all()


def test_a_duplicated_official_row_does_not_duplicate_bars(minute_block, official_eod):
    double = pd.concat([eod(official_eod), eod(official_eod, value=99.0)],
                       ignore_index=True)
    out = attach_official_eod(bars_frame(minute_block), double)
    assert len(out) == 3
    assert out["oi_official_eod"].dropna().unique().tolist() == [OFFICIAL_OI]


def test_the_column_records_when_it_became_visible_and_who_published_it(minute_block,
                                                                       official_eod):
    out = attach_official_eod(bars_frame(minute_block), eod(official_eod))
    assert (out["oi_official_eod_visible_from"] == f"{DAY}T15:30:00+05:30").all()
    assert out["oi_official_eod_source"].str.contains("bhavcopy").all()
    assert "post-session" in out["oi_official_eod_source"].iloc[0]


def test_the_close_can_be_moved_but_the_rule_still_holds(minute_block, official_eod):
    """A caller testing an earlier cut-off must not silently gain intraday visibility."""
    out = attach_official_eod(bars_frame(minute_block, labels=["09:15", "15:30"]),
                             eod(official_eod), session_close=time(16, 0))
    assert out["oi_official_eod"].isna().all()
    assert (out["oi_official_eod_visible_from"] == f"{DAY}T16:00:00+05:30").all()
    assert NSE_SESSION_CLOSE == time(15, 30)


@pytest.mark.parametrize("expiry", [DEFAULT_EXPIRY, "29-Jan-2026",
                                    "2026-01-29 00:00:00"])
def test_the_two_sources_write_the_identity_differently_and_still_join(minute_block,
                                                                      official_eod,
                                                                      expiry):
    out = attach_official_eod(bars_frame(minute_block),
                              eod(official_eod, expiry=expiry))
    assert out["oi_official_eod"].dropna().tolist() == [OFFICIAL_OI]


def test_case_and_type_of_the_identity_are_normalised_on_both_sides(minute_block,
                                                                   official_eod):
    frame = bars_frame(minute_block).assign(option_type="ce", strike="24000")
    out = attach_official_eod(frame, eod(official_eod, option_type="CE", strike="24000"))
    assert out["oi_official_eod"].dropna().tolist() == [OFFICIAL_OI]


def test_a_frame_without_the_join_keys_is_refused_not_silently_unmatched(minute_block,
                                                                        official_eod):
    with pytest.raises(ValueError, match="missing join keys"):
        attach_official_eod(minute_block().drop(columns=["expiry"]), eod(official_eod))
    with pytest.raises(ValueError, match=r"eod=\['expiry'\]"):
        attach_official_eod(bars_frame(minute_block),
                            official_eod([(DAY, DEFAULT_EXPIRY, 24000, "CE", 1.0)])
                            .drop(columns=["expiry"]))


def test_a_non_numeric_official_figure_becomes_a_missing_value_not_a_crash(minute_block,
                                                                          official_eod):
    out = attach_official_eod(bars_frame(minute_block),
                              eod(official_eod, value="not-a-number"))
    assert out["oi_official_eod"].isna().all()


# ---------------------------------------------------------------------------
# The guard, including against a deliberate leak
# ---------------------------------------------------------------------------
def test_the_guard_passes_on_a_frame_built_by_the_attacher(minute_block, official_eod):
    assert_no_eod_leakage(attach_official_eod(bars_frame(minute_block),
                                              eod(official_eod)))


def test_the_guard_catches_a_deliberately_leaked_figure(minute_block, official_eod):
    out = attach_official_eod(bars_frame(minute_block), eod(official_eod))
    # what a naive pipeline would do: back-fill the session with the figure published
    # after it ended, which is exactly the leak the guard exists to catch
    out["oi_official_eod"] = out["oi_official_eod"].bfill()
    with pytest.raises(LookAheadError, match="before 15:30:00"):
        assert_no_eod_leakage(out)


def test_the_guard_reports_how_many_bars_are_tainted(minute_block, official_eod):
    out = attach_official_eod(bars_frame(minute_block, labels=["09:15", "09:16", "15:30"]),
                             eod(official_eod))
    out.loc[0, "oi_official_eod"] = OFFICIAL_OI
    out.loc[1, "oi_official_eod"] = OFFICIAL_OI
    with pytest.raises(LookAheadError, match="2 bars carry oi_official_eod"):
        assert_no_eod_leakage(out)


def test_the_guard_only_checks_the_columns_it_is_asked_about(minute_block, official_eod):
    out = attach_official_eod(bars_frame(minute_block), eod(official_eod))
    out["strategy_oi"] = [OFFICIAL_OI, OFFICIAL_OI, OFFICIAL_OI]
    assert_no_eod_leakage(out)                                  # not listed: not checked
    with pytest.raises(LookAheadError, match="strategy_oi"):
        assert_no_eod_leakage(out, eod_columns=("oi_official_eod", "strategy_oi"))


def test_a_column_that_is_absent_is_not_invented_by_the_guard(minute_block):
    assert_no_eod_leakage(minute_block(), eod_columns=("oi_official_eod",))


def test_a_one_second_before_the_close_is_still_intraday(minute_block, official_eod):
    frame = bars_frame(minute_block, labels=["15:29"])
    frame["timestamp"] = frame["timestamp"] - pd.Timedelta(seconds=1)
    out = attach_official_eod(frame, eod(official_eod))
    assert out["oi_official_eod"].isna().all()
    assert_no_eod_leakage(out)


# ---------------------------------------------------------------------------
# No manufacture: the provider's own OI survives every normalisation step
# ---------------------------------------------------------------------------
def test_the_providers_own_open_interest_is_never_replaced(minute_block, official_eod):
    frame = bars_frame(minute_block)
    before = frame["open_interest"].copy()
    out = attach_official_eod(frame, eod(official_eod))
    pd.testing.assert_series_equal(out["open_interest"], before, check_names=False)
    # and the official figure sits in its own column, never merged into the provider's
    assert "oi_official_eod" in out.columns
    assert not np.allclose(out["open_interest"].to_numpy(), OFFICIAL_OI)


def test_a_bar_without_a_provider_oi_observation_is_left_without_one(minute_block,
                                                                     official_eod):
    """35.37% of real bars carry OI == 0: a missing observation, not a closed position."""
    frame = bars_frame(minute_block, labels=["09:15", "15:30"])
    frame["open_interest"] = [0.0, 0.0]
    out = attach_official_eod(frame, eod(official_eod))
    assert out["open_interest"].tolist() == [0.0, 0.0]
    normalised = normalise_units(out, LotTable(FLAT_65))
    assert normalised["oi_share_equivalent"].tolist() == [0.0, 0.0]
    assert normalised["oi_unit"].tolist() == [UNIT_PROVIDER_OI_LATE] * 2
    # the official figure is visible at the close, but never written into the series
    attached = normalised["oi_official_eod"].tolist()
    assert np.isnan(attached[0])
    assert attached[1] == pytest.approx(OFFICIAL_OI)
    assert_no_eod_leakage(normalised)


def test_normalisation_then_attachment_keeps_both_figures_separate(minute_block,
                                                                   official_eod):
    frame = bars_frame(minute_block, labels=["09:15", "15:30"])
    frame["open_interest"] = [900.0, 900.0]
    normalised = normalise_units(frame, LotTable(FLAT_65))
    out = attach_official_eod(normalised, eod(official_eod))
    assert out["oi_raw"].tolist() == [900.0, 900.0]
    assert out["oi_share_equivalent"].tolist() == [90_000.0, 90_000.0]
    assert out["oi_official_eod"].tolist()[1] == OFFICIAL_OI
    assert out["oi_official_eod"].isna().sum() == 1
    assert set(EOD_JOIN_KEYS) <= set(out.columns)
    assert_no_eod_leakage(out)


def test_the_derived_series_is_auditable_back_to_the_raw_one(minute_block):
    """Provenance on every row, so an analyst can tell estimated from observed."""
    frame = bars_frame(minute_block, labels=["09:15", "15:30"])
    frame["open_interest"] = [12_345.0, 12_345.0]
    out = normalise_units(frame, LotTable(FLAT_65))
    assert out["oi_raw"].tolist() == [12_345.0, 12_345.0]
    assert out["oi_share_equivalent"].tolist() == [1_234_500.0, 1_234_500.0]
    assert out["oi_scale_estimated"].tolist() == [True, True]
    assert "estimated" in out["unit_provenance"].iloc[0]
