"""T9: volume and open-interest unit normalisation.

Covers the milestone areas *volume conversion*, *OI conversion* and the two rules that
make them safe: raw provider columns are never overwritten, and every derived number
carries its unit, its basis and an honest ``estimated`` flag.

Every input here is a hand-written number, so the arithmetic in each assertion can be
re-derived by eye: 7,500 share-equivalents at lot 75 are 100 contracts, provider OI of
37,900 on 2025-01-01 is 3,790,000 share-equivalents *estimated*, and so on.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from nifty_quant.derivatives.reference import ContractLot, LotRegime, LotTable
from nifty_quant.derivatives.units import (
    OFFICIAL_ARBITER_FLOOR,
    OI_SCALE_ADJUSTMENT,
    OI_SCALE_BREAK,
    UNIT_CONTRACTS,
    UNIT_PROVIDER_OI_EARLY,
    UNIT_PROVIDER_OI_LATE,
    UNIT_SHARE_EQUIVALENT,
    normalise_units,
    oi_to_share_equivalent,
    share_equivalent_to_contracts,
    share_volume_to_contracts,
    units_verified,
)

# One flat regime so the frame tests below exercise conversion, not lot archaeology.
FLAT_75 = (LotRegime(75, date(2000, 1, 1), date(2199, 12, 31), "test regime"),)


def bars(*, days: list[str], volumes: list[float], oi: list[float]) -> pd.DataFrame:
    """One bar per (day, volume, OI) triple at 09:15, in the provider's own columns."""
    return pd.DataFrame({
        "symbol": ["NIFTY"] * len(days),
        "trading_day": days,
        "expiry": [f"{d[:4]}-{d[5:7]}-29" for d in days],
        "strike": [24000] * len(days),
        "option_type": ["CE"] * len(days),
        "timestamp": pd.to_datetime([f"{d} 09:15:00" for d in days]).tz_localize(
            "Asia/Kolkata"),
        "open": [100.0] * len(days), "high": [101.0] * len(days),
        "low": [99.0] * len(days), "close": [100.0] * len(days),
        "volume": volumes,
        "open_interest": oi,
    })


# ---------------------------------------------------------------------------
# Volume: share-equivalents -> contracts
# ---------------------------------------------------------------------------
def test_share_volume_to_contracts_divides_by_the_board_lot():
    conv = share_volume_to_contracts(7_500.0, lot=75)
    assert conv.value == 100.0
    assert conv.unit == UNIT_CONTRACTS
    assert conv.factor == pytest.approx(1.0 / 75.0)
    assert conv.integral is True
    assert conv.residual == 0.0
    assert conv.estimated is False
    assert "board lot 75" in conv.basis


def test_the_lot_plus_one_artifact_is_recorded_and_never_rounded_away():
    """The 09:15 artifact bars are ``lot*k+1``; 76 shares are not 1.01 contracts."""
    conv = share_volume_to_contracts(76.0, lot=75)
    assert conv.value == pytest.approx(76.0 / 75.0)
    assert conv.integral is False
    assert conv.residual == pytest.approx(1.0)


def test_a_short_bar_reports_a_negative_residual_on_the_nearest_side():
    """74 shares at lot 75 is one share *below* a contract, not 74 shares above zero."""
    conv = share_volume_to_contracts(74.0, lot=75)
    assert conv.integral is False
    assert conv.residual == pytest.approx(-1.0)


def test_a_zero_volume_bar_is_a_whole_number_of_contracts():
    conv = share_volume_to_contracts(0.0, lot=25)
    assert (conv.value, conv.integral, conv.residual) == (0.0, True, 0.0)


@pytest.mark.parametrize("lot", [0, -75])
def test_a_non_positive_lot_is_refused_rather_than_inverting_the_conversion(lot):
    with pytest.raises(ValueError, match="board lot must be positive"):
        share_volume_to_contracts(7_500.0, lot=lot)
    with pytest.raises(ValueError, match="board lot must be positive"):
        share_equivalent_to_contracts(7_500.0, lot=lot)


def test_official_share_equivalents_convert_with_the_same_rule():
    """``OpnIntrst`` is share-equivalents too, so 300 shares at lot 75 are 4 contracts."""
    conv = share_equivalent_to_contracts(300.0, lot=75)
    assert conv.value == 4.0
    assert conv.unit == UNIT_CONTRACTS


# ---------------------------------------------------------------------------
# Open interest: the January 2025 scale break
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("day,expected", [
    (date(2024, 12, 30), 1_234_500.0),
    (date(2024, 12, 31), 1_234_500.0),   # last pre-break day: identity
])
def test_oi_before_the_break_is_already_in_share_equivalents(day, expected):
    conv = oi_to_share_equivalent(1_234_500.0, day)
    assert conv.value == expected
    assert conv.unit == UNIT_SHARE_EQUIVALENT
    assert conv.factor == 1.0
    assert conv.estimated is False


@pytest.mark.parametrize("day", [date(2025, 1, 1), date(2025, 1, 2), date(2026, 11, 3)])
def test_oi_from_the_break_is_hundredfold_and_labelled_estimated(day):
    conv = oi_to_share_equivalent(12_345.0, day)
    assert conv.value == pytest.approx(1_234_500.0)
    assert conv.factor == OI_SCALE_ADJUSTMENT
    assert conv.unit == UNIT_SHARE_EQUIVALENT
    # A 100x adjustment inferred from daily totals is an estimate, never a fact.
    assert conv.estimated is True
    assert "estimated" in conv.basis
    assert OI_SCALE_BREAK.isoformat() in conv.basis


def test_the_scale_break_boundary_is_the_first_of_january_exactly():
    assert oi_to_share_equivalent(100.0, date(2024, 12, 31)).value == 100.0
    assert oi_to_share_equivalent(100.0, OI_SCALE_BREAK).value == 10_000.0


def test_a_missing_oi_observation_is_not_turned_into_a_number():
    """No-manufacture rule: NaN in, NaN out - never a filled zero."""
    conv = oi_to_share_equivalent(float("nan"), date(2025, 6, 2))
    assert np.isnan(conv.value)
    assert oi_to_share_equivalent(0.0, date(2025, 6, 2)).value == 0.0


@pytest.mark.parametrize("day,verified", [
    (date(2023, 12, 28), False),
    (date(2024, 1, 1), False),
    (OFFICIAL_ARBITER_FLOOR, True),      # 2024-01-02: first free bhavcopy
    (date(2026, 11, 3), True),
])
def test_units_are_only_claimed_verified_where_an_official_arbiter_exists(day, verified):
    assert units_verified(day) is verified


# ---------------------------------------------------------------------------
# Frame-level normalisation
# ---------------------------------------------------------------------------
def test_normalise_units_never_overwrites_a_raw_provider_column():
    frame = bars(days=["2025-06-02", "2024-06-03"], volumes=[7_500.0, 2_500.0],
                 oi=[12_345.0, 900_000.0])
    original = frame[["volume", "open_interest"]].copy()
    out = normalise_units(frame, LotTable(FLAT_75))

    pd.testing.assert_frame_equal(frame[["volume", "open_interest"]], original)
    pd.testing.assert_series_equal(out["volume"], original["volume"],
                                   check_names=False)
    pd.testing.assert_series_equal(out["open_interest"], original["open_interest"],
                                   check_names=False)
    # and keeps an explicitly named home for them
    pd.testing.assert_series_equal(out["oi_raw"], original["open_interest"],
                                   check_names=False)
    pd.testing.assert_series_equal(out["volume_share_equivalent"], original["volume"],
                                   check_names=False)


def test_normalise_units_adds_derivations_without_renaming_the_input():
    frame = bars(days=["2025-06-02"], volumes=[7_500.0], oi=[12_345.0])
    out = normalise_units(frame, LotTable(FLAT_75))
    for name in frame.columns:
        assert name in out.columns
    assert len(out) == len(frame)
    assert out["volume_contracts"].tolist() == [100.0]
    assert out["oi_share_equivalent"].tolist() == [1_234_500.0]
    assert out["oi_contracts"].tolist() == [pytest.approx(1_234_500.0 / 75.0)]


def test_the_estimated_flag_is_carried_row_by_row_across_the_break():
    out = normalise_units(
        bars(days=["2024-12-31", "2025-01-01"], volumes=[7_500.0, 7_500.0],
             oi=[900_000.0, 9_000.0]),
        LotTable(FLAT_75))
    assert out["oi_scale_estimated"].tolist() == [False, True]
    # The label describes the *provider's* column, not the derived one.
    assert out["oi_unit"].tolist() == [UNIT_PROVIDER_OI_EARLY, UNIT_PROVIDER_OI_LATE]
    assert out["oi_share_equivalent"].tolist() == [900_000.0, 900_000.0]


def test_lot_and_units_verified_are_reported_per_row():
    out = normalise_units(
        bars(days=["2023-12-28", "2025-06-02"], volumes=[3_750.0, 7_500.0],
             oi=[50_000.0, 500.0]),
        LotTable(FLAT_75))
    assert out["lot_size"].tolist() == [75, 75]
    assert out["lot_estimated"].tolist() == [True, True]
    assert out["lot_basis"].tolist() == ["date_regime_fallback"] * 2
    assert out["units_verified"].tolist() == [False, True]


def test_a_non_integral_volume_stays_visible_in_the_derived_column():
    out = normalise_units(bars(days=["2025-06-02"], volumes=[76.0], oi=[1.0]),
                          LotTable(FLAT_75))
    assert out["volume_integral"].tolist() == [False]
    assert out["volume_contracts"].tolist() == [pytest.approx(76.0 / 75.0)]


def test_an_unknown_lot_yields_no_number_rather_than_an_assumed_one():
    out = normalise_units(bars(days=["2025-06-02"], volumes=[7_500.0], oi=[12_345.0]),
                          LotTable(regimes=()))
    assert pd.isna(out["lot_size"].iloc[0])
    assert np.isnan(out["volume_contracts"].iloc[0])
    assert np.isnan(out["oi_contracts"].iloc[0])
    assert out["volume_integral"].tolist() == [False]
    assert out["lot_basis"].tolist() == ["unknown"]
    assert out["lot_estimated"].tolist() == [True]


def test_a_nan_volume_bar_does_not_break_the_normalisation_pass():
    out = normalise_units(bars(days=["2025-06-02", "2025-06-03"],
                               volumes=[float("nan"), 7_500.0], oi=[1.0, 1.0]),
                          LotTable(FLAT_75))
    assert np.isnan(out["volume_contracts"].iloc[0])
    assert out["volume_contracts"].iloc[1] == 100.0


def test_the_rows_own_official_lot_beats_the_date_regime_inside_the_frame():
    """A mixed-lot date: newly issued expiries carry 65 while older ones keep 75."""
    day = date(2025, 11, 3)
    table = LotTable(FLAT_75, contract_lots=[
        ContractLot(day=day, symbol="NIFTY", expiry="2025-11-03", strike=24000,
                    option_type="CE", lot=65)])
    frame = bars(days=["2025-11-03"], volumes=[6_500.0], oi=[65.0])
    frame["expiry"] = "2025-11-03"
    out = normalise_units(frame, table)
    assert out["lot_size"].tolist() == [65]
    assert out["lot_estimated"].tolist() == [False]
    assert out["volume_contracts"].tolist() == [100.0]
    # The very same contract one day later has no official row for that day: the
    # date regime is used and admits it is an estimate.
    frame2 = bars(days=["2025-11-04"], volumes=[6_500.0], oi=[65.0])
    frame2["expiry"] = "2025-11-03"
    out2 = normalise_units(frame2, table)
    assert out2["lot_size"].tolist() == [75]
    assert out2["lot_estimated"].tolist() == [True]
    assert out2["volume_contracts"].tolist() == [pytest.approx(6_500.0 / 75.0)]


def test_provenance_states_both_units_the_lot_and_its_basis():
    out = normalise_units(bars(days=["2025-01-15"], volumes=[7_500.0], oi=[120.0]),
                          LotTable(FLAT_75))
    text = out["unit_provenance"].iloc[0]
    assert "provider volume is share_equivalents" in text
    assert "lot 75 via date_regime_fallback" in text
    assert f"provider OI unit is {UNIT_PROVIDER_OI_LATE}" in text
    assert "-> share_equivalents via" in text


def test_normalisation_can_be_reapplied_to_its_own_output_without_drift():
    """Derived columns are additive: a second pass sees the same provider columns."""
    frame = bars(days=["2025-06-02"], volumes=[7_500.0], oi=[12_345.0])
    table = LotTable(FLAT_75)
    once = normalise_units(frame, table)
    twice = normalise_units(once, table)
    for column in ("volume", "open_interest", "volume_contracts",
                   "oi_share_equivalent", "oi_scale_estimated", "oi_unit"):
        pd.testing.assert_series_equal(once[column], twice[column], check_names=False)
