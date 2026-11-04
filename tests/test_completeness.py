"""T9: completeness classification and expiry-file verdicts.

A gap in this archive can mean at least six different things and they must not be
merged: a contract that did not trade, a contract that was not listed yet, a session
whose tail is missing, a day whose trades are missing, a day that is genuinely lost, and
an expiry the provider never published.  Each verdict below is the milestone's
"clearly distinguish" requirement written as a test, including the boundary each rule
turns on.

The thresholds are quoted as literals so changing one has to be a decision:
``SESSION_TAIL_FLOOR = "15:25"``, ``MIN_VOLUME_COVERAGE = 0.99``,
``TRUNCATION_TOLERANCE_DAYS = 3``.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from nifty_quant.derivatives.completeness import (
    DAY_INACTIVE,
    DAY_MISSING,
    DAY_MISSING_TRADES,
    DAY_NO_OI,
    DAY_NO_TRADES,
    DAY_PRESENT,
    DAY_TRUNCATED,
    FILE_ABSENT,
    FILE_PRESENT,
    FILE_TRUNCATED,
    MIN_VOLUME_COVERAGE,
    SESSION_TAIL_FLOOR,
    TRUNCATION_TOLERANCE_DAYS,
    FILE_NOT_IN_PROVIDER,
    classify_day,
    classify_file,
    summarise,
)

REFERENCE = date(2026, 11, 3)
EXPIRED = date(2026, 8, 4)
LIVE = date(2026, 12, 3)
DAY = "2026-07-01"


def day(**kwargs):
    """A traded, complete, fully observed contract-day - then break one thing at a time."""
    args = dict(trading_day=DAY, expiry="2026-08-04", strike=24000, option_type="CE",
                listed=True, official_volume=100.0, official_oi=5_000.0,
                archive_bars=250, archive_last_oi=5_000.0,
                expiry_date=EXPIRED, reference_date=REFERENCE,
                block_last_label="15:30", volume_coverage=1.0)
    args.update(kwargs)
    return classify_day(**args)


# ---------------------------------------------------------------------------
# Day x contract verdicts
# ---------------------------------------------------------------------------
def test_a_fully_observed_contract_day_is_simply_present():
    report = day()
    assert report.status == DAY_PRESENT
    assert report.reason == ""
    assert report.archive_bars == 250
    assert (report.block_last_label, report.volume_coverage) == ("15:30", 1.0)


@pytest.mark.parametrize("last_oi", [0.0, None])
def test_bars_without_an_open_interest_observation_are_not_a_missing_day(last_oi):
    """A zero here means the provider carried no observation, not that positions closed."""
    report = day(archive_last_oi=last_oi)
    assert report.status == DAY_NO_OI
    assert report.archive_bars == 250
    assert "no open-interest observation" in report.reason
    assert "official OI 5000.0" in report.reason


def test_a_session_that_stops_early_loses_its_tail_for_every_contract():
    """Measured on 2025-09-26: bars end at 15:00 and 9.7% of the day's volume is absent."""
    report = day(block_last_label="15:00", volume_coverage=0.903)
    assert report.status == DAY_TRUNCATED
    assert "15:00" in report.reason
    assert SESSION_TAIL_FLOOR in report.reason


@pytest.mark.parametrize("label,truncated", [
    ("15:30", False), (SESSION_TAIL_FLOOR, False),   # the floor itself is a full session
    ("15:24", True), ("14:59", True), ("09:40", True),
])
def test_the_tail_floor_is_the_1525_minute(label, truncated):
    assert day(block_last_label=label).status == (DAY_TRUNCATED if truncated
                                                  else DAY_PRESENT)


def test_an_empty_label_is_not_claimed_to_be_a_truncation():
    """A block whose last label was never measured is judged on what is known."""
    assert day(block_last_label="").status == DAY_PRESENT


@pytest.mark.parametrize("coverage,status", [
    (1.0, DAY_PRESENT),
    (MIN_VOLUME_COVERAGE, DAY_PRESENT),        # 0.99 is the floor, not below it
    (0.9899, DAY_MISSING_TRADES),
    (0.9031, DAY_MISSING_TRADES),
    (0.5, DAY_MISSING_TRADES),
])
def test_the_session_can_be_whole_and_still_be_missing_trades(coverage, status):
    report = day(volume_coverage=coverage)
    assert report.status == status
    if status == DAY_MISSING_TRADES:
        assert f"{100 * coverage:.2f}%" in report.reason
        assert str(int(MIN_VOLUME_COVERAGE * 100)) in report.reason


def test_truncation_is_reported_before_a_volume_shortfall():
    """One cause per cell: the missing minutes explain the missing trades."""
    assert day(block_last_label="15:00", volume_coverage=0.4).status == DAY_TRUNCATED


def test_a_missing_open_interest_observation_is_the_last_thing_checked():
    assert day(volume_coverage=0.4, archive_last_oi=0.0).status == DAY_MISSING_TRADES


@pytest.mark.parametrize("volume,oi", [(0.0, None), (0.0, 0.0), (0.0, 5_000.0),
                                       (None, None)])
def test_zero_official_volume_is_an_absence_of_trades_not_lost_data(volume, oi):
    """The archive only holds minutes that traded, whatever the open interest says."""
    report = day(archive_bars=0, official_volume=volume, official_oi=oi)
    assert report.status == DAY_NO_TRADES
    assert "absence of bars is correct" in report.reason


def test_a_traded_day_with_no_bars_at_all_is_genuine_lost_data():
    report = day(archive_bars=0, official_volume=100.0, block_last_label="",
                 volume_coverage=None)
    assert report.status == DAY_MISSING
    assert "100 contracts traded" in report.reason
    assert report.archive_bars == 0


def test_a_live_contract_that_has_not_traded_yet_is_inactive_not_missing():
    report = day(archive_bars=0, official_volume=100.0, expiry_date=LIVE)
    assert report.status == DAY_INACTIVE
    assert "expiry not yet reached" in report.reason


def test_an_unlisted_contract_is_inactive_and_carries_no_official_claims():
    report = day(listed=False, archive_bars=0, official_volume=None, official_oi=None)
    assert report.status == DAY_INACTIVE
    assert (report.official_volume, report.official_oi) == (None, None)
    assert "lists no row" in report.reason


def test_the_identity_and_the_day_are_echoed_in_iso_and_never_reformatted():
    report = day(trading_day=pd.Timestamp("2026-07-01 09:15:00"),
                 expiry="2026-08-04 00:00:00", strike="24000")
    assert (report.trading_day, report.expiry, report.strike, report.option_type) == (
        "2026-07-01", "2026-08-04", 24000, "CE")


def test_summarise_counts_each_verdict_separately():
    reports = [day(), day(archive_last_oi=0.0), day(block_last_label="15:00"),
               day(archive_bars=0, official_volume=0.0), day(archive_bars=0),
               day(listed=False, archive_bars=0), day(volume_coverage=0.5)]
    counts = summarise(reports)
    assert counts == {DAY_PRESENT: 1, DAY_NO_OI: 1, DAY_TRUNCATED: 1,
                      DAY_NO_TRADES: 1, DAY_MISSING: 1, DAY_INACTIVE: 1,
                      DAY_MISSING_TRADES: 1}
    assert sum(counts.values()) == len(reports)


# ---------------------------------------------------------------------------
# Expiry-file verdicts
# ---------------------------------------------------------------------------
def a_file(**kwargs):
    args = dict(symbol="NIFTY", expiry="2026-08-04", provider_file_present=True,
                rows=1_000_000, days_observed=25, first_day="2026-06-24",
                last_day="2026-08-03", official_days_expected=26,
                reference_date=REFERENCE, expiry_date=EXPIRED)
    args.update(kwargs)
    return classify_file(**args)


def test_a_file_that_runs_up_to_its_expiry_is_present_and_reports_its_gap():
    report = a_file()
    assert report.status == FILE_PRESENT
    assert report.days_missing == 1
    assert "25 observed of 26 expected" in report.details


def test_a_file_that_stops_well_before_its_own_expiry_day_is_truncated():
    """The 2026-08-04 file: 2,646 rows ending 2026-07-02, nothing after."""
    report = a_file(rows=2_646, days_observed=6, last_day="2026-07-02",
                    official_days_expected=26)
    assert report.status == FILE_TRUNCATED
    assert report.days_missing == 20
    assert "stops on 2026-07-02" in report.details


@pytest.mark.parametrize("last_day,truncated", [
    ("2026-08-02", False),
    (str(EXPIRED - timedelta(days=TRUNCATION_TOLERANCE_DAYS)), False),  # exactly 3
    ("2026-07-31", True), ("2026-06-30", True),
])
def test_the_truncation_tolerance_is_three_calendar_days(last_day, truncated):
    assert a_file(last_day=last_day).status == (FILE_TRUNCATED if truncated
                                               else FILE_PRESENT)


def test_a_live_expiry_is_never_called_truncated_for_a_session_that_has_not_happened():
    report = a_file(expiry_date=LIVE, last_day="2026-06-30",
                    expiry="2026-12-03")
    assert report.status == FILE_PRESENT


def test_a_file_with_no_last_day_cannot_be_judged_for_truncation():
    assert a_file(last_day="", rows=0, days_observed=0).status == FILE_PRESENT


def test_an_expiry_the_exchange_listed_but_the_provider_never_published_is_a_hole():
    report = a_file(provider_file_present=False, rows=0, days_observed=0,
                    first_day="", last_day="", official_days_expected=18)
    assert report.status == FILE_ABSENT
    assert report.days_missing == 18
    assert "the provider returns no file" in report.details


def test_an_expiry_with_neither_a_file_nor_official_evidence_is_not_reported_as_a_hole():
    report = a_file(provider_file_present=False, official_days_expected=0)
    assert report.status == FILE_NOT_IN_PROVIDER
    assert "no official evidence" in report.details


def test_days_missing_never_goes_negative_when_the_file_exceeds_the_official_days():
    assert a_file(days_observed=40, official_days_expected=26).days_missing == 0


def test_the_file_verdict_keeps_the_expiry_in_the_same_form_as_the_day_verdict():
    assert a_file(expiry=pd.Timestamp("2026-08-04"),
                  expiry_date=EXPIRED).expiry == "2026-08-04"
