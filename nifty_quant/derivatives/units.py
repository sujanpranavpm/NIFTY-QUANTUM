"""Unit normalisation for the provider archive: traded volume and open interest.

What the audits established (``logs/m2_volume_units.txt``, ``logs/m3_oi_norm.txt``,
``logs/lot_schedule.txt``), measured against the official NSE F&O bhavcopy on 310
file-days spanning 263 distinct trading dates:

* provider ``volume`` is **share equivalents** = contracts x board lot, where the lot is
  the one listed for *that contract row*.  The match is exact (summed absolute
  difference 0, 100% of contracts) on the 13 lot-65 file-days (1,477 contracts) and on
  the 7 mixed lot-65/75 file-days of 2025-11 (1,303 contracts), and the median is exact
  on the lot-75 dates with 0.000% non-integral minute bars.  On the lot-25/lot-50 2024
  dates the median row ratio is 1.00036/1.00002 and 0.5-1.6% of minute bars are not a
  whole multiple of the lot: an opening-minute artifact of ``lot*k+1`` shares
  concentrated at 09:15, so a day total can sit 1-2 shares above the official figure and
  a low-volume contract can also read more than 1% away in either direction (726 above,
  169 below across all tested contract-days).  The single largest residual, 724 million
  shares on 2025-09-26, is not a unit error at all: that file-day is a truncated session
  ending at 15:00, and is reported by ``completeness`` instead.
* provider ``open_interest`` is share equivalents up to **2024-12-31** and
  **one hundredth of that from 2025-01-01** onward.  Median raw official/provider ratios
  are 0.996-1.001 before the break and 99.877-100.003 after it, on 262 official dates,
  with no date contradicting its side of the break.
* in the official bhavcopy itself ``TtlTradgVol`` is *contracts* and ``OpnIntrst`` is
  *share equivalents* - the two columns are not in the same unit.
* rupee turnover cannot arbitrate any of this: ``TtlTrfVal`` for options is a
  notional/strike-based figure rather than premium x shares, so its ratio against the
  archive's own rupee value (median 0.0016) carries no unit information.

Rules enforced here: raw provider columns are never overwritten, every derived number
carries its unit and the basis on which it was obtained, the 100x OI adjustment is
flagged as *estimated*, and official end-of-day figures are attached only at or after
the session close so they cannot leak into intraday decisions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, time

import numpy as np
import pandas as pd

from nifty_quant.derivatives.reference import (
    BASIS_DATE_REGIME,
    LotTable,
    LotResolution,
    expiry_from_iso,
)
from nifty_quant.derivatives.timestamps import clock_labels

# ---------------------------------------------------------------------------
# Unit vocabulary
# ---------------------------------------------------------------------------
UNIT_SHARE_EQUIVALENT = "share_equivalent"
UNIT_CONTRACTS = "contracts"
UNIT_PROVIDER_OI_EARLY = "share_equivalent"
UNIT_PROVIDER_OI_LATE = "one_hundredth_share_equivalent"

#: First calendar day whose provider open interest is 1/100 of share equivalents.
OI_SCALE_BREAK = date(2025, 1, 1)
#: Multiply provider OI by this from ``OI_SCALE_BREAK`` on to obtain share equivalents.
OI_SCALE_ADJUSTMENT = 100.0
#: Earliest date the official F&O bhavcopy archive reaches, hence the earliest date any
#: unit claim is independently verified.  Before it, conversions are applied but flagged.
OFFICIAL_ARBITER_FLOOR = date(2024, 1, 2)

NSE_SESSION_CLOSE = time(15, 30)

#: Relative tolerance under which a converted provider OI that agrees with the official
#: end-of-day figure is *independently verified* (``logs/m3_oi_norm.txt`` used 1%).
OI_VERIFY_TOLERANCE = 0.01


class LookAheadError(ValueError):
    """An official end-of-day figure was attached to a bar before the session closed."""


class AmbiguousLotError(ValueError):
    """A contract-level P&L needs a specific board lot but only a date-only fallback
    is available on a date that listed more than one lot - converting there would price
    the contract with the wrong lot, so the conversion is refused rather than guessed."""


@dataclass(frozen=True)
class Conversion:
    """One normalised number with the reasoning that produced it."""

    value: float
    unit: str
    factor: float
    estimated: bool
    basis: str
    integral: bool = True
    residual: float = 0.0

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Scalar conversions
# ---------------------------------------------------------------------------
def share_volume_to_contracts(volume: float, lot: int) -> Conversion:
    """Provider volume (share equivalents) -> traded contracts.

    A non-integral result is recorded, never rounded away: on the 2024 dates the
    opening-minute artifact bars are ``lot*k+1`` and must stay visible so strict
    validation can reject them instead of silently accepting a fraction of a contract.
    """
    if lot <= 0:
        raise ValueError(f"board lot must be positive, got {lot}")
    whole = int(volume) // lot
    residual = float(volume) - whole * lot
    return Conversion(
        value=float(volume) / lot, unit=UNIT_CONTRACTS, factor=1.0 / lot,
        estimated=False, basis=f"divided by board lot {lot}",
        integral=residual == 0.0, residual=residual if residual <= lot / 2 else residual - lot)


def oi_to_share_equivalent(raw_oi: float, day: date) -> Conversion:
    """Provider open interest -> share equivalents, date-aware across the scale break."""
    if day >= OI_SCALE_BREAK:
        return Conversion(
            value=float(raw_oi) * OI_SCALE_ADJUSTMENT, unit=UNIT_SHARE_EQUIVALENT,
            factor=OI_SCALE_ADJUSTMENT, estimated=True,
            basis=f"provider OI is 1/100 of share equivalents from "
                  f"{OI_SCALE_BREAK.isoformat()}; adjustment is estimated")
    return Conversion(value=float(raw_oi), unit=UNIT_SHARE_EQUIVALENT, factor=1.0,
                      estimated=False, basis="provider OI already in share equivalents")


def share_equivalent_to_contracts(shares: float, lot: int) -> Conversion:
    """Share equivalents (either source) -> contracts."""
    if lot <= 0:
        raise ValueError(f"board lot must be positive, got {lot}")
    return Conversion(value=float(shares) / lot, unit=UNIT_CONTRACTS, factor=1.0 / lot,
                      estimated=False, basis=f"divided by board lot {lot}")


def units_verified(day: date) -> bool:
    """Whether this day's units were checked against an independent official arbiter."""
    return day >= OFFICIAL_ARBITER_FLOOR


def _row_date(ts) -> date:
    return pd.Timestamp(ts).date()


# ---------------------------------------------------------------------------
# Frame-level normalisation
# ---------------------------------------------------------------------------
DERIVED_COLUMNS = (
    "raw_volume", "lot_size", "lot_basis", "lot_estimated",
    "volume_share_equivalent", "volume_contracts", "volume_integral",
    "conversion_basis", "conversion_estimated",
    "raw_oi", "oi_raw", "oi_share_equivalent", "oi_scale_estimated", "oi_contracts",
    "oi_unit", "oi_verified", "oi_verified_flag", "units_verified", "unit_provenance",
)


def normalise_units(frame: pd.DataFrame, lot_table: LotTable, *,
                    symbol: str = "NIFTY",
                    timestamp_col: str = "timestamp",
                    volume_col: str = "volume",
                    oi_col: str = "open_interest",
                    keys_col_prefix: bool = True) -> pd.DataFrame:
    """Return a copy of ``frame`` with normalised columns added.

    The provider's own ``volume`` and ``open_interest`` columns are carried through
    unchanged; ``oi_raw`` and ``volume_share_equivalent`` exist so the raw values keep
    an explicit, named home even if a later stage renames or drops the originals.  A
    missing board lot yields ``NaN`` derived values rather than an assumed lot.
    """
    out = frame.copy()
    days = out[timestamp_col].map(_row_date)
    lots: list[LotResolution] = []
    if keys_col_prefix and {"expiry", "strike", "option_type"} <= set(out.columns):
        for day, exp, strike, opt in zip(days, out["expiry"], out["strike"],
                                         out["option_type"]):
            lots.append(lot_table.lot_for(day, symbol, str(exp)[:10], int(strike),
                                          str(opt).upper()))
    else:
        for day in days:
            lots.append(lot_table.lot_on(day))

    lot_sizes = [res.lot for res in lots]
    out["raw_volume"] = out[volume_col]
    out["lot_size"] = lot_sizes
    out["lot_basis"] = [res.basis for res in lots]
    out["lot_estimated"] = [res.estimated for res in lots]
    # Provenance of the volume -> contracts conversion is the lot and how it was known.
    out["conversion_basis"] = [res.basis for res in lots]
    out["conversion_estimated"] = [res.estimated for res in lots]

    vol = out[volume_col].astype(float)
    contracts = [share_volume_to_contracts(v, lot) if lot and pd.notna(v) else None
                 for v, lot in zip(vol, lot_sizes)]
    out["volume_share_equivalent"] = vol
    out["volume_contracts"] = [c.value if c else float("nan") for c in contracts]
    out["volume_integral"] = [bool(c.integral) if c else False for c in contracts]

    oi_conv = [oi_to_share_equivalent(raw, day) for raw, day in
               zip(out[oi_col].astype(float), days)]
    out["raw_oi"] = out[oi_col]
    out["oi_raw"] = out[oi_col]
    out["oi_share_equivalent"] = [c.value for c in oi_conv]
    out["oi_scale_estimated"] = [c.estimated for c in oi_conv]
    out["oi_contracts"] = [share_equivalent_to_contracts(c.value, lot).value
                           if lot else float("nan") for c, lot in zip(oi_conv, lot_sizes)]
    # The unit label always describes the *provider's* column, not the derived one.
    out["oi_unit"] = [UNIT_PROVIDER_OI_LATE if day >= OI_SCALE_BREAK
                      else UNIT_PROVIDER_OI_EARLY for day in days]
    out["units_verified"] = [units_verified(day) for day in days]
    out["unit_provenance"] = [
        f"provider volume is share_equivalents (contracts = value / lot); "
        f"lot {lot_value} via {lot_basis}; "
        f"provider OI unit is {oi_unit} -> share_equivalents via {oi_basis}"
        for lot_value, lot_basis, oi_basis, oi_unit in zip(
            lot_sizes, [res.basis for res in lots],
            [c.basis for c in oi_conv], out["oi_unit"])
    ]
    # The verification columns are populated by mark_verified_oi(); defaulting them here
    # means a normalised frame always carries them, and an unverified row stays NaN/False
    # rather than being absent (so a filter on oi_verified_flag cannot silently pass).
    out["oi_verified"] = float("nan")
    out["oi_verified_flag"] = False
    return out


# ---------------------------------------------------------------------------
# Official end-of-day joins - leakage control
# ---------------------------------------------------------------------------
EOD_JOIN_KEYS = ("trading_day", "expiry", "strike", "option_type")


def _expiry_key(value):
    """Expiry in the one representation both sources can be written in.

    ``pandas.NaT`` passes through unmatched: a contract whose expiry cannot be read
    is left without an official figure rather than joined to the wrong one.
    """
    if value is None or pd.isna(value):
        return None
    return expiry_from_iso(value)


def _normalised_keys(frame: pd.DataFrame, prefix: str = "") -> pd.DataFrame:
    """Cast the join keys to one representation on both sides of a merge."""
    keys = frame[list(EOD_JOIN_KEYS)].copy()
    keys["trading_day"] = pd.to_datetime(keys["trading_day"], errors="coerce").dt.strftime("%Y-%m-%d")
    # The exchange writes ``26-Dec-2024`` and the provider writes ``2024-12-26``; both
    # have to reach the same key, which a plain string slice would not achieve.
    keys["expiry"] = keys["expiry"].map(_expiry_key)
    keys["strike"] = pd.to_numeric(keys["strike"], errors="coerce").astype("float64")
    keys["option_type"] = keys["option_type"].astype(str).str.upper()
    return keys.add_prefix(prefix)


def attach_official_eod(frame: pd.DataFrame, eod: pd.DataFrame,
                        eod_column: str = "OpnIntrst",
                        target_column: str = "oi_official_eod",
                        session_close: time = NSE_SESSION_CLOSE,
                        stamp_column: str = "timestamp") -> pd.DataFrame:
    """Join official end-of-day figures onto bars *at or after* the session close.

    ``eod`` must hold one row per contract per trading day with the ``EOD_JOIN_KEYS``
    columns and ``eod_column``.  Bars before the close receive ``NaN``: an end-of-day
    figure published after the market closed cannot inform a decision taken during the
    session, and the provider's own intraday ticks are the only honest source for them.
    """
    missing_frame = set(EOD_JOIN_KEYS) - set(frame.columns)
    missing_eod = set(EOD_JOIN_KEYS) - set(eod.columns)
    if missing_frame or missing_eod:
        raise ValueError(f"missing join keys frame={sorted(missing_frame)} "
                         f"eod={sorted(missing_eod)}")
    out = frame.copy()
    # Replace the key columns on both sides with one normalised representation so the
    # merge is a clean many-to-one join and the row count is preserved.
    left = pd.concat([_normalised_keys(out), out.drop(columns=list(EOD_JOIN_KEYS))], axis=1)
    right = eod.copy()
    right = pd.concat([_normalised_keys(right),
                       right[[eod_column]].apply(pd.to_numeric, errors="coerce")], axis=1)
    right = right.drop_duplicates(subset=list(EOD_JOIN_KEYS), keep="first")
    merged = left.merge(right[list(EOD_JOIN_KEYS) + [eod_column]],
                        on=list(EOD_JOIN_KEYS), how="left")
    # The provider stamps bars in IST, so the wall clock is read as written - a
    # conversion to UTC would move every 15:30 close to 10:00 and break the guard.
    clock = clock_labels(out[stamp_column], "%H:%M:%S")
    close_iso = session_close.strftime("%H:%M:%S")
    # ``merged`` is a left join, so its rows stay in ``out``'s order; its index is a fresh
    # RangeIndex, hence the values are moved positionally and re-attached to ``out``'s index
    # before the mask is applied by label.
    eod_values = pd.Series(merged[eod_column].to_numpy(dtype="float64"), index=out.index)
    out[target_column] = eod_values.where(clock >= close_iso)
    day_iso = pd.to_datetime(out["trading_day"], errors="coerce").dt.strftime("%Y-%m-%d")
    out[f"{target_column}_visible_from"] = day_iso + f"T{close_iso}+05:30"
    out[f"{target_column}_source"] = "NSE official F&O bhavcopy (EOD, post-session)"
    return out


def assert_no_eod_leakage(frame: pd.DataFrame, eod_columns=("oi_official_eod",),
                          stamp_column: str = "timestamp",
                          close: time = NSE_SESSION_CLOSE) -> None:
    """Raise unless every populated official column sits at or after the close."""
    close_iso = close.strftime("%H:%M:%S")
    clock = clock_labels(frame[stamp_column], "%H:%M:%S")
    for name in eod_columns:
        if name not in frame.columns:
            continue
        bad = frame.loc[frame[name].notna() & (clock < close_iso)]
        if len(bad):
            raise LookAheadError(
                f"{len(bad)} bars carry {name} before {close_iso}; official EOD data "
                f"must not be visible to intraday decisions")


# ---------------------------------------------------------------------------
# Independent OI verification
# ---------------------------------------------------------------------------
def mark_verified_oi(frame: pd.DataFrame, *, eod_column: str = "oi_official_eod",
                     converted_column: str = "oi_share_equivalent",
                     tolerance: float = OI_VERIFY_TOLERANCE,
                     stamp_column: str = "timestamp",
                     keys: tuple[str, ...] = EOD_JOIN_KEYS) -> pd.DataFrame:
    """Add ``oi_verified`` / ``oi_verified_flag`` from an independent EOD cross-check.

    A (contract x day) is *independently verified* only when its last intraday converted
    open-interest observation agrees with the official end-of-day figure within
    ``tolerance``.  Verified rows repeat the normalised value in ``oi_verified``; every
    other row stays ``NaN`` so an OI-dependent feature can restrict itself to verified
    observations instead of trusting the estimate.  The raw and normalised columns are
    never modified here, and nothing is filled: ``attach_official_eod`` must have run
    first, otherwise no row carries an official figure and nothing is verified.
    """
    out = frame.copy()
    if eod_column not in out.columns or converted_column not in out.columns:
        out["oi_verified"] = float("nan")
        out["oi_verified_flag"] = False
        return out
    if "oi_verified" not in out.columns:
        out["oi_verified"] = float("nan")
    if "oi_verified_flag" not in out.columns:
        out["oi_verified_flag"] = False
    group_keys = [k for k in keys if k in out.columns]
    if not group_keys:
        return out
    clock = pd.to_datetime(out[stamp_column], utc=True, format="mixed")
    for _, grp in out.groupby(group_keys, sort=False, dropna=False):
        official = grp.loc[grp[eod_column].notna(), eod_column]
        if official.empty:
            continue  # no official arbiter for this contract-day -> stays unverified
        eod_value = float(official.iloc[-1])
        # The last bar of the session carries the day's final provider OI observation.
        grp_clock = clock.loc[grp.index]
        last_index = grp_clock.idxmax()
        last_converted = out.loc[last_index, converted_column]
        if not (np.isfinite(eod_value) and eod_value > 0
                and pd.notna(last_converted)
                and abs(float(last_converted) - eod_value) <= tolerance * max(eod_value, 1.0)):
            continue
        out.loc[grp.index, "oi_verified"] = out.loc[grp.index, converted_column]
        out.loc[grp.index, "oi_verified_flag"] = True
    return out


# ---------------------------------------------------------------------------
# Contract-level P&L lot guard
# ---------------------------------------------------------------------------
def contract_lot_for_pnl(lot_table: LotTable, day: date, symbol: str = "NIFTY",
                         expiry: str | None = None, strike: int | None = None,
                         option_type: str | None = None) -> LotResolution:
    """Resolve the lot for a *contract-level realised P&L*, refusing an ambiguous one.

    A date-only lot fallback is safe for a coarse statistic but not for pricing a
    specific contract: on the 61 mixed-lot dates it is wrong for a minority of
    contracts.  When the only available basis is the date regime and that date listed
    more than one lot, this raises instead of silently mispricing the position.
    """
    res = lot_table.lot_for(day, symbol, expiry, strike, option_type)
    if not res.known:
        raise AmbiguousLotError(
            f"no board lot is available for {symbol} {expiry} {strike} {option_type} "
            f"on {day.isoformat()}; a contract-level P&L cannot be priced")
    if res.basis == BASIS_DATE_REGIME and len(lot_table.observed_lots_on(day, symbol)) > 1:
        raise AmbiguousLotError(
            f"{day.isoformat()} listed lots {lot_table.observed_lots_on(day, symbol)}; "
            f"a date-only fallback must not price a contract-level P&L for "
            f"{expiry} {strike} {option_type}")
    return res
