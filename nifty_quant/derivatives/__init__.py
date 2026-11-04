"""Index-derivative reference data and unit/timestamp normalisation.

This package holds the corrections that must be applied to the raw provider archive
before it can be used for research.  It never rewrites raw files: every function
returns a copy with derived columns and explicit provenance next to the untouched
provider values.

``reference``      historical board lots and contract identity
``units``          volume and open-interest unit conversion, EOD leakage guards
``timestamps``     the resolved candle convention and strict session classification
``candles``        5/15-minute aggregation, allowed only after the convention is fixed
``completeness``   missing file vs inactive contract vs missing trades vs truncation
"""

from __future__ import annotations

from nifty_quant.derivatives import candles, completeness, reference, timestamps, units
from nifty_quant.derivatives.candles import (
    AggregationResult,
    aggregate,
    aggregate_contract,
    candle_index,
    candle_label,
    minute_index,
    verdicts_by_block,
)
from nifty_quant.derivatives.completeness import (
    DAY_INACTIVE,
    DAY_MISSING,
    DAY_NO_OI,
    DAY_NO_TRADES,
    DAY_PRESENT,
    FILE_ABSENT,
    FILE_PRESENT,
    FILE_TRUNCATED,
    DayReport,
    FileReport,
    classify_day,
    classify_file,
    summarise,
)
from nifty_quant.derivatives.reference import (
    BASIS_DATE_REGIME,
    BASIS_OFFICIAL_ROW,
    BASIS_UNKNOWN,
    CONTRACT_KEYS,
    ContractLot,
    LotRegime,
    LotResolution,
    LotTable,
    contract_key,
    expiry_from_iso,
    nifty_lot_regimes,
)
from nifty_quant.derivatives.timestamps import (
    AMBIGUOUS_NO_TRUSTED_FIRST_BUCKET,
    AMBIGUOUS_OPENING_BUCKET,
    AMBIGUOUS_OUT_OF_SESSION,
    AMBIGUOUS_TERMINAL_BUCKET,
    CONVENTION,
    CONVENTION_EVIDENCE,
    NO_CLOSING_PRINT,
    SESSION_CLOSE,
    SESSION_OPEN,
    STRICT_OK,
    SessionVerdict,
    bar_interval,
    classify_session,
    clock_label,
    clock_labels,
    contains_trade,
    first_last_bucket,
    label_set,
    minute_volume_profile,
    session_close_stamp,
)
from nifty_quant.derivatives.units import (
    OFFICIAL_ARBITER_FLOOR,
    OI_SCALE_ADJUSTMENT,
    OI_SCALE_BREAK,
    UNIT_CONTRACTS,
    UNIT_PROVIDER_OI_EARLY,
    UNIT_PROVIDER_OI_LATE,
    UNIT_SHARE_EQUIVALENT,
    Conversion,
    LookAheadError,
    assert_no_eod_leakage,
    attach_official_eod,
    normalise_units,
    oi_to_share_equivalent,
    share_equivalent_to_contracts,
    share_volume_to_contracts,
    units_verified,
)

__all__ = [
    "AMBIGUOUS_NO_TRUSTED_FIRST_BUCKET", "AMBIGUOUS_OPENING_BUCKET",
    "AMBIGUOUS_OUT_OF_SESSION", "AMBIGUOUS_TERMINAL_BUCKET", "AggregationResult",
    "BASIS_DATE_REGIME", "BASIS_OFFICIAL_ROW", "BASIS_UNKNOWN",
    "CONTRACT_KEYS", "CONVENTION", "CONVENTION_EVIDENCE", "Conversion", "ContractLot",
    "DAY_INACTIVE", "DAY_MISSING", "DAY_NO_OI", "DAY_NO_TRADES", "DAY_PRESENT",
    "FILE_ABSENT", "FILE_PRESENT", "FILE_TRUNCATED", "DayReport", "FileReport",
    "LookAheadError", "LotRegime", "LotResolution", "LotTable", "NO_CLOSING_PRINT",
    "OFFICIAL_ARBITER_FLOOR", "OI_SCALE_ADJUSTMENT", "OI_SCALE_BREAK", "SESSION_CLOSE",
    "SESSION_OPEN", "STRICT_OK", "SessionVerdict", "UNIT_CONTRACTS",
    "UNIT_PROVIDER_OI_EARLY", "UNIT_PROVIDER_OI_LATE", "UNIT_SHARE_EQUIVALENT",
    "aggregate", "aggregate_contract", "assert_no_eod_leakage", "attach_official_eod",
    "bar_interval", "candle_index", "candle_label", "classify_day", "classify_file",
    "classify_session", "clock_label", "clock_labels",
    "contains_trade", "contract_key", "candles", "completeness", "expiry_from_iso",
    "first_last_bucket", "label_set", "minute_index", "minute_volume_profile",
    "nifty_lot_regimes", "normalise_units", "oi_to_share_equivalent",
    "reference", "share_equivalent_to_contracts", "share_volume_to_contracts",
    "summarise", "timestamps", "units", "units_verified", "verdicts_by_block",
]
