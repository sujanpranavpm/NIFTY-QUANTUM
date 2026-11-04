"""T9: historical board-lot mapping and contract/expiry identity.

The NIFTY board lot changed five times inside the archive window and, at every change,
the exchange listed two lots on the *same calendar day* (new lot for newly issued
expiries, old lot for everything already listed).  A date-only lot is therefore a
labelled fallback, never the primary source, and these tests pin both the measured
regime boundaries and the authority rule.

Boundaries are the modal-lot transitions measured from the cached bhavcopies in
``logs/lot_schedule.txt``; the provenance strings record where each one is inferred
rather than observed.
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from nifty_quant.derivatives.reference import (
    BASIS_DATE_REGIME,
    BASIS_OFFICIAL_ROW,
    BASIS_UNKNOWN,
    ContractLot,
    LotRegime,
    LotTable,
    contract_key,
    expiry_from_iso,
    nifty_lot_regimes,
)

#: The five modal regimes, indexed in chronological order.
REGIMES = nifty_lot_regimes()


def lot_on(day: date) -> int:
    return LotTable().lot_on(day).lot


# ---------------------------------------------------------------------------
# The measured historical schedule
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("day,expected", [
    (date(2021, 5, 27), 75),
    (date(2021, 11, 30), 75),   # last day of the inferred pre-arbiter regime
    (date(2021, 12, 1), 50),    # lot cut to 50
    (date(2024, 4, 25), 50),    # last official date still on lot 50
    (date(2024, 4, 29), 50),
    (date(2024, 4, 30), 25),    # lot cut to 25
    (date(2024, 11, 21), 25),   # last date lot 25 was the only lot listed
    (date(2024, 12, 19), 25),
    (date(2024, 12, 20), 75),   # lot raised back to 75
    (date(2025, 10, 29), 75),   # last date lot 75 was the only lot listed
    (date(2025, 12, 23), 75),
    (date(2025, 12, 24), 65),   # lot cut to 65
    (date(2026, 1, 5), 65),
    (date(2026, 11, 3), 65),    # still current
])
def test_every_historical_lot_boundary_resolves_to_the_measured_lot(day, expected):
    assert lot_on(day) == expected


def test_the_regimes_are_ordered_contiguous_and_cover_every_trading_day():
    previous = None
    for regime in REGIMES:
        assert regime.observed_first <= regime.observed_last
        if previous is not None:
            # Contiguous: no calendar day falls between two regimes unclaimed.
            assert (regime.observed_first - previous.observed_last).days == 1
            assert regime.lot != previous.lot
        previous = regime
    for day in [date(2021, 1, 4), date(2024, 1, 1), date(2026, 11, 3)]:
        assert LotTable().lot_on(day).known


def test_a_date_only_lookup_is_always_an_estimate():
    """No regime boundary is an observed effective date, so every fallback estimates.

    The single authoritative answer a ``LotTable`` can give comes from an official row.
    """
    for day in [date(2021, 5, 27), date(2024, 6, 3), date(2026, 11, 3)]:
        resolution = LotTable().lot_on(day)
        assert resolution.estimated is True
        assert resolution.basis == BASIS_DATE_REGIME


def test_pre_arbiter_windows_say_so_in_their_provenance():
    """The free official archive starts 2024-01-02; anything reaching before it is
    inferred from the provider's own minimum positive traded volume and must say so."""
    archive_floor = date(2024, 1, 2)
    for regime in REGIMES:
        reaches_pre_arbiter_era = regime.observed_first < archive_floor
        admits_inference = ("NO OFFICIAL ARBITER" in regime.provenance
                            or "inferred" in regime.provenance)
        assert admits_inference or not reaches_pre_arbiter_era
    assert "NO OFFICIAL ARBITER" in LotTable().lot_on(date(2021, 5, 27)).provenance
    # wholly inside the official era, and claiming nothing but observation
    assert "inferred" not in LotTable().lot_on(date(2024, 6, 3)).provenance


def test_a_day_outside_every_regime_is_reported_as_unknown():
    table = LotTable(regimes=(LotRegime(25, date(2024, 4, 30), date(2024, 12, 19), "x"),))
    resolution = table.lot_on(date(2025, 6, 2))
    assert resolution.lot is None
    assert not resolution.known
    assert resolution.basis == BASIS_UNKNOWN
    assert "no lot regime covers 2025-06-02" in resolution.provenance


def test_at_a_bracketed_boundary_the_tightest_regime_claims_the_day():
    """Overlapping windows only exist while the true effective date is being bracketed."""
    table = LotTable(regimes=(
        LotRegime(25, date(2024, 11, 1), date(2024, 12, 31), "wide, older lot"),
        LotRegime(75, date(2024, 12, 20), date(2024, 12, 22), "tight, newly listed lot"),
    ))
    assert table.lot_on(date(2024, 12, 21)).lot == 75
    assert table.lot_on(date(2024, 12, 19)).lot == 25


# ---------------------------------------------------------------------------
# Authority: the official per-row lot always wins
# ---------------------------------------------------------------------------
def mixed_table() -> LotTable:
    day = date(2024, 12, 25)
    return LotTable(contract_lots=[
        ContractLot(day=day, symbol="NIFTY", expiry="2024-12-26", strike=24000,
                    option_type="CE", lot=25, provenance=f"{BASIS_OFFICIAL_ROW} {day}"),
        ContractLot(day=day, symbol="NIFTY", expiry="2025-01-02", strike=24000,
                    option_type="CE", lot=75, provenance=f"{BASIS_OFFICIAL_ROW} {day}"),
    ])


def test_two_lots_on_one_date_are_resolved_per_contract_not_per_date():
    table = mixed_table()
    day = date(2024, 12, 25)
    older = table.lot_for(day, "NIFTY", "2024-12-26", 24000, "CE")
    newer = table.lot_for(day, "NIFTY", "2025-01-02", 24000, "CE")
    assert (older.lot, newer.lot) == (25, 75)
    assert older.estimated is False and newer.estimated is False
    assert older.basis.startswith(BASIS_OFFICIAL_ROW)
    # The date-only fallback is the *modal* lot, so it is right for the newly issued
    # expiry and a 3x over-division for the older one: hence estimated=True on it.
    assert table.lot_on(day).lot == 75
    assert table.lot_on(day).estimated is True


def test_an_incomplete_contract_identity_falls_back_instead_of_guessing():
    table = mixed_table()
    day = date(2024, 12, 25)
    # No expiry, or no strike: the official row cannot be reached, so the answer is the
    # date regime and it is labelled as such.
    assert table.lot_for(day, "NIFTY", expiry=None, strike=24000,
                         option_type="CE").lot == 75
    assert table.lot_for(day, "NIFTY", "2024-12-26", strike=None,
                         option_type="CE").estimated is True
    assert table.lot_for(day, "NIFTY", "2024-12-26", 24000, "CE").lot == 25


def test_an_unknown_contract_on_a_known_date_uses_the_date_regime():
    table = mixed_table()
    resolution = table.lot_for(date(2024, 12, 25), "NIFTY", "2099-01-01", 25000, "PE")
    assert (resolution.lot, resolution.estimated) == (75, True)


def test_observed_lots_on_exposes_a_mixed_date():
    table = mixed_table()
    assert table.observed_lots_on(date(2024, 12, 25)) == (25, 75)
    assert table.observed_lots_on(date(2024, 12, 26)) == ()
    assert table.observed_lots_on(date(2024, 12, 25), symbol="BANKNIFTY") == ()


def test_a_later_observation_replaces_an_earlier_one_for_the_same_key():
    table = mixed_table()
    day = date(2024, 12, 25)
    table.add(ContractLot(day=day, symbol="NIFTY", expiry="2024-12-26", strike=24000,
                          option_type="CE", lot=75))
    assert table.lot_for(day, "NIFTY", "2024-12-26", 24000, "CE").lot == 75


# ---------------------------------------------------------------------------
# Construction from a real bhavcopy shape
# ---------------------------------------------------------------------------
def official_rows() -> pd.DataFrame:
    """The four column names the NSE F&O bhavcopy actually uses, plus junk rows."""
    return pd.DataFrame({
        "XpryDt": ["26-Dec-2024", "26-Dec-2024", "26-Dec-2024", "26-Dec-2024",
                   "26-Dec-2024", "2024-12-26"],
        "StrkPric": ["24000.00", 24000, "24050.00", 24100.0, "24150.00", "24200.00"],
        "OptnTp": ["ce", "CE", "Ce", "PE", "CE", "CE"],
        "NewBrdLotQty": [25, 25.0, "25", None, "0", 75],
    })


def test_from_official_rows_keeps_only_positive_lots_and_normalises_the_identity():
    table = LotTable.from_official_rows(official_rows(), day=date(2024, 12, 24))
    assert table.observed_lots_on(date(2024, 12, 24)) == (25, 75)
    for strike, option_type in [(24000, "CE"), (24050, "CE"), (24200, "CE")]:
        assert table.lot_for(date(2024, 12, 24), "NIFTY", "2024-12-26",
                             strike, option_type).known
    # the blank and zero lots were skipped, not defaulted
    assert not table.lot_for(date(2024, 12, 24), "NIFTY", "2024-12-26",
                             24100, "PE").basis.startswith(BASIS_OFFICIAL_ROW)


def test_a_bhavcopy_expiry_and_a_provider_expiry_reach_the_same_row():
    """The exchange writes ``26-Dec-2024``; the provider writes ``2024-12-26``."""
    table = LotTable.from_official_rows(official_rows(), day=date(2024, 12, 24))
    for expiry in ["2024-12-26", "26-Dec-2024", date(2024, 12, 26),
                   pd.Timestamp("2024-12-26"), "2024-12-26 00:00:00"]:
        for option_type in ["CE", "ce"]:
            resolution = table.lot_for(date(2024, 12, 24), "NIFTY", expiry, 24000,
                                       option_type)
            assert (resolution.lot, resolution.estimated) == (25, False), expiry


# ---------------------------------------------------------------------------
# Expiry and contract identity mapping
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value,expected", [
    (date(2025, 12, 24), "2025-12-24"),
    (datetime(2025, 12, 24, 15, 30), "2025-12-24"),
    (pd.Timestamp("2025-12-24 00:00:00"), "2025-12-24"),
    (pd.Timestamp("2025-12-24 09:15:00", tz="Asia/Kolkata"), "2025-12-24"),
    ("2025-12-24", "2025-12-24"),
    ("2025-12-24 00:00:00", "2025-12-24"),
    ("24-Dec-2025", "2025-12-24"),
    ("24-Dec-2025 00:00:00", "2025-12-24"),
])
def test_expiry_from_iso_always_returns_a_bare_date(value, expected):
    assert expiry_from_iso(value) == expected


@pytest.mark.parametrize("value", ["", "not-an-date", "24-XX-2025"])
def test_an_unparsable_expiry_is_an_error_not_a_silent_passthrough(value):
    with pytest.raises(ValueError, match="unparsable expiry"):
        expiry_from_iso(value)


def test_contract_key_is_the_identity_every_provider_can_agree_on():
    assert contract_key("NIFTY", "2025-12-24 00:00:00", "24000", "ce") == (
        "NIFTY", "2025-12-24", 24000, "CE")
    keys = {contract_key("NIFTY", "2025-12-24", 24000, "CE"),
             contract_key("NIFTY", "2025-12-24 00:00:00", 24000.0, "ce")}
    assert len(keys) == 1


def test_the_regime_set_is_supplied_in_memory_and_read_from_nothing():
    """Reference data is handed to the table by the audit scripts, never fetched here."""
    table = LotTable(regimes=(LotRegime(65, date(2025, 12, 24), date(2199, 12, 31),
                                        "current"),))
    assert table.lot_on(date(2026, 11, 3)).lot == 65
    assert not table.lot_on(date(2024, 6, 3)).known
    # and the default set is the measured one, not a hard-coded single lot
    assert LotTable().lot_on(date(2024, 6, 3)).lot == 25
