"""Stage 2 tests: the effective-dated historical reference database.

Two layers are tested:

* synthetic stores - hand-written windows with hand-countable dates, so the
  resolution rules (tightest window wins, fallback always estimated, unknown
  raises) are verified by arithmetic, not by trusting the built CSVs;
* the built ``data/reference`` CSVs - the real effective-dated facts the repo
  ships, checked against the milestone's hard rules (mixed-lot dates return
  more than one lot, the expiry weekday changed on 2025-09-01, the tick was
  verified across regimes, and a date outside every record raises rather than
  inheriting today's specification).
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from nifty_quant.derivatives.calendar import (
    NOT_TRADING,
    TRADING,
    UNKNOWN,
    WEEKEND,
    AmbiguousExpiryError,
    EffectiveParameter,
    ExpirySchedule,
    NiftyReference,
    ParameterStore,
    TradingCalendar,
    UnknownParameterError,
    reference_dir,
)
from nifty_quant.derivatives.reference import (
    REFERENCE_DIR,
    nifty_expiry_weekday,
    nifty_lot_regimes,
)

BUILT = REFERENCE_DIR / "nifty_reference_parameters.csv"
pytestmark = pytest.mark.skipif(
    not BUILT.exists(), reason="data/reference not built (scripts/_p2_build_reference.py)")


@pytest.fixture(scope="module")
def ref() -> NiftyReference:
    return NiftyReference.from_directory(reference_dir())


def ep(parameter: str, start: date, end: date, value, provenance: str = "test",
       estimated: bool = False) -> EffectiveParameter:
    return EffectiveParameter(parameter, start, end, value, provenance, estimated)


# ---------------------------------------------------------------------------
# ParameterStore resolution rules (synthetic)
# ---------------------------------------------------------------------------
def test_tightest_covering_window_wins():
    store = ParameterStore([
        ep("board_lot", date(2020, 1, 1), date(2030, 12, 31), 75, "wide regime", True),
        ep("board_lot", date(2024, 5, 1), date(2024, 6, 1), 25, "narrow observation"),
    ])
    res = store.resolve("board_lot", date(2024, 5, 20))
    assert res.value == 25
    assert res.exact is True
    assert res.estimated is False


def test_a_non_estimated_record_beats_an_estimated_one_of_equal_span():
    store = ParameterStore([
        ep("board_lot", date(2024, 1, 1), date(2024, 12, 31), 50, "guess", True),
        ep("board_lot", date(2024, 1, 1), date(2024, 12, 31), 25, "observed"),
    ])
    assert store.resolve("board_lot", date(2024, 7, 1)).value == 25


def test_a_day_outside_every_window_raises_without_a_fallback():
    store = ParameterStore([ep("price_tick", date(2024, 1, 1), date(2024, 12, 31), 0.05)])
    with pytest.raises(UnknownParameterError, match="no 'price_tick' record covers"):
        store.resolve("price_tick", date(2019, 12, 31))


def test_a_fallback_is_answered_with_estimated_never_as_fact():
    store = ParameterStore([ep("price_tick", date(2024, 1, 1), date(2024, 12, 31), 0.05)])
    fallback = ep("price_tick", date(2019, 1, 1), date(2019, 12, 31), 0.05,
                  "caller's guess", True)
    res = store.resolve("price_tick", date(2019, 6, 1), fallback=fallback)
    assert res.value == 0.05
    assert res.exact is False
    assert res.estimated is True


def test_empty_effective_to_reads_as_open_ended(tmp_path):
    csv = tmp_path / "params.csv"
    csv.write_text(
        "parameter,effective_from,effective_to,value,provenance,estimated\n"
        "board_lot,2025-12-24,,65,still current,false\n", encoding="utf-8")
    store = ParameterStore.from_csv(csv)
    res = store.resolve("board_lot", date(2099, 1, 1))
    assert res.value == 65 and res.estimated is False


# ---------------------------------------------------------------------------
# Trading calendar (synthetic)
# ---------------------------------------------------------------------------
def calendar_fixture() -> TradingCalendar:
    days = [date(2024, 8, 26), date(2024, 8, 27), date(2024, 8, 28),
            date(2024, 8, 30), date(2024, 9, 3), date(2024, 9, 4)]
    return TradingCalendar(days, provenance="test calendar",
                           floor=date(2024, 8, 1), ceiling=date(2024, 9, 30))


def test_weekday_inside_span_without_a_file_is_a_derived_non_trading_day():
    cal = calendar_fixture()
    assert cal.status_of(date(2024, 8, 29)).status == NOT_TRADING   # in-span weekday
    assert cal.status_of(date(2024, 8, 28)).status == TRADING


def test_weekend_and_out_of_span_days_are_never_claimed_known():
    cal = calendar_fixture()
    assert cal.status_of(date(2024, 8, 31)).status == WEEKEND       # a Saturday
    assert cal.status_of(date(2024, 8, 31)).estimated is True       # never a fact
    assert cal.status_of(date(2023, 6, 15)).status == UNKNOWN       # before floor
    assert cal.status_of(date(2025, 6, 16)).status == UNKNOWN       # after ceiling
    assert cal.is_trading_day(date(2023, 6, 15)) is None


def test_previous_and_next_trading_day_and_month_end():
    cal = calendar_fixture()
    assert cal.previous_trading_day(date(2024, 8, 31)) == date(2024, 8, 30)
    # The fixture deliberately omits Monday 2024-09-02 (Ganesh Chaturthi), so the
    # next trading day after the 31st skips the weekend *and* the holiday Monday.
    assert cal.next_trading_day(date(2024, 8, 31)) == date(2024, 9, 3)
    assert cal.last_trading_day_of_month(2024, 8) == date(2024, 8, 30)
    assert cal.previous_trading_day(date(2024, 8, 2)) is None       # before all days


def test_mislabelled_weekday_csv_is_refused(tmp_path):
    csv = tmp_path / "days.csv"
    csv.write_text("date,weekday,provenance\n2024-08-28,Fri,test\n", encoding="utf-8")
    with pytest.raises(ValueError, match="labelled Fri but is a Wed"):
        TradingCalendar.from_csv(csv)


# ---------------------------------------------------------------------------
# Expiry cadence (synthetic): the weekday changed - and only the rule knows when
# ---------------------------------------------------------------------------
def cadence_store() -> ParameterStore:
    return ParameterStore([
        ep("expiry_weekly_weekday", date(2021, 1, 1), date(2025, 8, 31), "Thu",
           "circular FAOP68747 (before)"),
        ep("expiry_weekly_weekday", date(2025, 9, 1), date(2027, 12, 31), "Tue",
           "circular FAOP68747 (after)"),
    ])


def test_expiry_weekday_differs_either_side_of_2025_09_01():
    sched = ExpirySchedule(cadence_store())
    assert sched.weekday_of(date(2025, 8, 28)) == "Thu"     # one day before
    assert sched.weekday_of(date(2025, 9, 4)) == "Tue"      # one week after
    with pytest.raises(UnknownParameterError):
        sched.weekday_of(date(2035, 1, 4))                  # beyond every record


def test_weekday_rule_and_reference_module_agree():
    sched = ExpirySchedule(cadence_store())
    for day in (date(2022, 3, 3), date(2025, 8, 31), date(2025, 9, 1), date(2026, 7, 2)):
        name, _prov, estimated = nifty_expiry_weekday(day)
        assert sched.weekday_of(day) == name
        assert estimated is False


def test_holiday_shift_resolves_to_the_preceding_trading_day():
    sched = ExpirySchedule(cadence_store())
    cal = calendar_fixture()
    # Thursday 2024-08-29 was a non-trading day in this synthetic calendar.
    resolved, shifted = sched.resolve_expiry_date(date(2024, 8, 29), cal)
    assert (resolved, shifted) == (date(2024, 8, 28), True)
    # A nominal day that traded does not move.
    assert sched.resolve_expiry_date(date(2024, 8, 28), cal) == (date(2024, 8, 28), False)


def test_monthly_is_tested_before_weekly_and_only_the_calendar_can_prove_it():
    sched = ExpirySchedule(cadence_store())
    cal = calendar_fixture()
    # August's last trading day was Friday the 30th - after the last Thursday.
    assert sched.cadence_of("2024-08-30", cal) == "monthly_last_trading_day"
    assert sched.cadence_of("2024-08-22", cal) == "weekly_nominal"   # mid-month Thu
    # 2024-08-28 is a trading Wednesday whose nominal Thursday 2024-08-29 was a
    # non-trading day: the calendar alone proves the weekly expiry shifted back.
    assert sched.cadence_of("2024-08-28", cal) == "weekly_holiday_shift"
    # Without a calendar, weekday facts survive but monthly cannot be proven.
    assert sched.cadence_of("2024-08-22") == "weekly_nominal"


def test_ambiguous_contract_mapping_raises_instead_of_guessing():
    # Two listed Thursdays seven days apart (the 22nd and the 29th).  The 22nd
    # traded, and the whole run from the 23rd to the 29th was non-trading, so the
    # holiday-shift rule maps BOTH nominals back onto the single trading day the
    # 22nd.  One trading day then owns two contract identities, and the resolver
    # must refuse rather than prefer one.
    sched = ExpirySchedule(cadence_store(), observed=[
        ("2024-08-22", {}), ("2024-08-29", {})])
    cal = TradingCalendar([date(2024, 8, 22), date(2024, 9, 10)],
                          provenance="test calendar",
                          floor=date(2024, 8, 1), ceiling=date(2024, 9, 30))
    with pytest.raises(AmbiguousExpiryError):
        sched.expiry_for_trading_day(date(2024, 8, 22), cal)


def test_unobserved_trading_day_maps_to_no_expiry_rather_than_a_fabricated_one():
    sched = ExpirySchedule(cadence_store(), observed=[("2024-08-29", {})])
    cal = calendar_fixture()
    assert sched.expiry_for_trading_day(date(2024, 9, 3), cal) == []


# ---------------------------------------------------------------------------
# The built reference database (real CSVs)
# ---------------------------------------------------------------------------
def test_the_store_carries_every_milestone_parameter(ref):
    names = {r.parameter for r in ref.store.records}
    assert {"board_lot", "price_tick", "price_step_slab", "strike_interval",
            "expiry_weekly_weekday"} <= names


def test_lot_regime_boundaries_resolve_on_both_sides(ref):
    # 2024-04-29 (last lot-50 day) versus 2024-04-30 (first lot-25 day).
    assert ref.board_lot(date(2024, 4, 29)).value == 50
    assert ref.board_lot(date(2024, 4, 30)).value == 25


def test_mixed_lot_dates_offer_more_than_one_lot(ref):
    """Measured reality: 61 cached dates carried two lots; a date-only answer there
    is a guess, so both windows must stay visible to the caller."""
    both_dec = {r.value for r in ref.store.covering("board_lot", date(2024, 12, 24))}
    assert both_dec == {25, 75}
    both_nov = {r.value for r in ref.store.covering("board_lot", date(2025, 11, 3))}
    assert both_nov == {65, 75}


def test_observed_era_lots_are_facts_and_inferred_era_lots_are_estimates(ref):
    inside = ref.board_lot(date(2024, 8, 15))
    assert inside.exact and inside.estimated is False
    assert inside.record.provenance.startswith("observed:")
    before = ref.board_lot(date(2023, 6, 15))
    assert before.value == 50
    assert before.estimated is True          # no arbiter before 2024-01-02
    assert "inferred" in before.record.provenance


def test_weekday_regime_change_survives_into_the_built_store(ref):
    assert ref.expiries.weekday_of(date(2025, 8, 28)) == "Thu"
    assert ref.expiries.weekday_of(date(2025, 9, 2)) == "Tue"


def test_tick_is_one_verified_window_and_cites_all_regimes(ref):
    records = ref.regime_records("price_tick")
    assert len(records) == 1
    rec = records[0]
    assert rec.value == 0.05 and rec.estimated is False
    assert rec.effective_from <= date(2021, 5, 27)      # first archive file
    assert rec.effective_to >= date(2026, 11, 3)        # last official date
    assert "100% on the 0.05 grid" in rec.provenance
    assert ref.tick_size(date(2022, 3, 3)).value == 0.05


def test_a_date_before_the_archive_raises_instead_of_inheriting_todays_spec(ref):
    with pytest.raises(UnknownParameterError):
        ref.tick_size(date(1999, 1, 1))
    with pytest.raises(UnknownParameterError):
        ref.strike_interval(date(1999, 1, 1))


def test_trading_calendar_matches_the_cache_and_flags_unknowns(ref):
    assert len(ref.calendar.days) == 318
    assert date(2024, 1, 26) not in ref.calendar.days             # Republic Day
    assert ref.calendar.status_of(date(2024, 1, 26)).status == NOT_TRADING
    assert ref.calendar.status_of(date(2023, 1, 26)).status == UNKNOWN
    # Functional-session Saturdays the exchange really traded, cached as such:
    assert ref.calendar.is_trading_day(date(2024, 1, 20)) is True
    assert ref.calendar.is_trading_day(date(2025, 2, 1)) is True
    # An ordinary Saturday has no arbiter, so it is UNKNOWN, never a guessed fact.
    assert ref.calendar.is_trading_day(date(2024, 6, 15)) is None


def test_every_lot_regime_value_is_resolvable_at_its_midpoint(ref):
    for reg in nifty_lot_regimes():
        mid = reg.observed_first + (reg.observed_last - reg.observed_first) / 2
        res = ref.board_lot(mid)
        available = {r.value for r in ref.store.covering("board_lot", mid)}
        assert reg.lot == res.value or reg.lot in available


def test_strike_intervals_are_measured_ladder_minimums(ref):
    rec = ref.strike_interval(date(2025, 3, 10))
    assert rec.value == 1000                       # dense-ladder era
    later = ref.strike_interval(date(2026, 8, 3))
    assert later.value == 50                       # dense 50-point ladders, 2026
    assert rec.estimated is False


def test_built_csvs_are_sorted_and_deterministically_readable(ref):
    frame = pd.read_csv(BUILT, dtype=str, keep_default_na=False)
    assert list(frame["parameter"]) == sorted(frame["parameter"], key=str)
    # Re-reading from the default directory yields the same facts.
    again = NiftyReference.from_directory()
    assert again.board_lot(date(2024, 8, 15)).value == ref.board_lot(date(2024, 8, 15)).value
    assert len(again.calendar.days) == len(ref.calendar.days)
