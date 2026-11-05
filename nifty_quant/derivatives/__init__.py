"""Index-derivative reference data and unit/timestamp normalisation.

This package holds the corrections that must be applied to the raw provider archive
before it can be used for research.  It never rewrites raw files: every function
returns a copy with derived columns and explicit provenance next to the untouched
provider values.

``reference``      historical board lots and contract identity
``calendar``       effective-dated reference DB: trading days, expiries, lots, ticks
``units``          volume and open-interest unit conversion, EOD leakage guards
``timestamps``     the resolved candle convention and strict session classification
``candles``        5/15-minute aggregation, allowed only after the convention is fixed
``completeness``   missing file vs inactive contract vs missing trades vs truncation
"""

from __future__ import annotations

from nifty_quant.derivatives import (
    calendar, candles, completeness, coverage_registry, reference, timestamps, units)
from nifty_quant.derivatives.calendar import (
    NOT_TRADING,
    TRADING,
    WEEKEND,
    AmbiguousExpiryError,
    DayStatus,
    EffectiveParameter,
    ExpirySchedule,
    NiftyReference,
    ParameterResolution,
    ParameterStore,
    TradingCalendar,
    UnknownParameterError,
)
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
    DAY_MISSING_TRADES,
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
from nifty_quant.derivatives.coverage_registry import (
    INACTIVE_CONTRACT,
    MISSING_CANDLE,
    MISSING_CONTRACT,
    MISSING_FILE,
    NO_TRADE,
    PRESENT as COVERAGE_PRESENT,
    PRESENT_WITHOUT_OI,
    TRUNCATED_SESSION,
    TRUNCATED_TAIL,
    UNKNOWN,
    CoverageEntry,
    CoverageRegistry,
    high_low_within_official,
    missing_candle_minutes,
    registry_status_for_day,
    registry_status_for_file,
)
from nifty_quant.derivatives.reference import (
    BASIS_DATE_REGIME,
    BASIS_OFFICIAL_ROW,
    BASIS_UNKNOWN,
    CONTRACT_KEYS,
    EXPIRY_WEEKDAY_REGIMES,
    REFERENCE_DIR,
    ContractLot,
    LotRegime,
    LotResolution,
    LotTable,
    contract_key,
    expiry_from_iso,
    nifty_expiry_weekday,
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
    duplicate_timestamps,
    first_last_bucket,
    is_closing_print,
    label_set,
    minute_volume_profile,
    session_boundary_violations,
    session_close_stamp,
)
from nifty_quant.derivatives.units import (
    OFFICIAL_ARBITER_FLOOR,
    OI_SCALE_ADJUSTMENT,
    OI_SCALE_BREAK,
    OI_VERIFY_TOLERANCE,
    UNIT_CONTRACTS,
    UNIT_PROVIDER_OI_EARLY,
    UNIT_PROVIDER_OI_LATE,
    UNIT_SHARE_EQUIVALENT,
    AmbiguousLotError,
    Conversion,
    LookAheadError,
    assert_no_eod_leakage,
    attach_official_eod,
    contract_lot_for_pnl,
    mark_verified_oi,
    normalise_units,
    oi_to_share_equivalent,
    share_equivalent_to_contracts,
    share_volume_to_contracts,
    units_verified,
)

__all__ = [
    "AMBIGUOUS_NO_TRUSTED_FIRST_BUCKET", "AMBIGUOUS_OPENING_BUCKET",
    "AMBIGUOUS_OUT_OF_SESSION", "AMBIGUOUS_TERMINAL_BUCKET", "AggregationResult",
    "AmbiguousLotError", "AmbiguousExpiryError",
    "BASIS_DATE_REGIME", "BASIS_OFFICIAL_ROW", "BASIS_UNKNOWN",
    "CONTRACT_KEYS", "CONVENTION", "CONVENTION_EVIDENCE", "Conversion", "ContractLot",
    "DAY_INACTIVE", "DAY_MISSING", "DAY_NO_OI", "DAY_NO_TRADES", "DAY_PRESENT",
    "DAY_MISSING_TRADES", "DAY_TRUNCATED",
    "DayReport", "DayStatus", "EXPIRY_WEEKDAY_REGIMES", "EffectiveParameter", "ExpirySchedule",
    "FILE_ABSENT", "FILE_PRESENT", "FILE_TRUNCATED", "FileReport",
    "INACTIVE_CONTRACT", "MISSING_CANDLE", "MISSING_CONTRACT", "MISSING_FILE",
    "NO_TRADE", "PRESENT_WITHOUT_OI", "TRUNCATED_SESSION", "TRUNCATED_TAIL",
    "UNKNOWN", "CoverageEntry", "CoverageRegistry",
    "LookAheadError", "LotRegime", "LotResolution", "LotTable",
    "NOT_TRADING", "NO_CLOSING_PRINT",
    "NiftyReference", "OFFICIAL_ARBITER_FLOOR", "OI_SCALE_ADJUSTMENT",
    "OI_SCALE_BREAK", "OI_VERIFY_TOLERANCE",
    "ParameterResolution", "ParameterStore", "REFERENCE_DIR",
    "SESSION_CLOSE",
    "SESSION_OPEN", "STRICT_OK", "SessionVerdict", "TRADING", "UNIT_CONTRACTS",
    "UNIT_PROVIDER_OI_EARLY", "UNIT_PROVIDER_OI_LATE", "UNIT_SHARE_EQUIVALENT",
    "UnknownParameterError", "WEEKEND",
    "aggregate", "aggregate_contract", "assert_no_eod_leakage", "attach_official_eod",
    "bar_interval", "calendar", "candle_index", "candle_label", "classify_day",
    "classify_file", "classify_session", "clock_label", "clock_labels",
    "contract_lot_for_pnl",
    "contains_trade", "contract_key", "candles", "completeness", "coverage_registry",
    "duplicate_timestamps", "expiry_from_iso",
    "first_last_bucket", "high_low_within_official", "is_closing_print", "label_set",
    "mark_verified_oi", "minute_index", "missing_candle_minutes",
    "minute_volume_profile", "nifty_expiry_weekday",
    "registry_status_for_day", "registry_status_for_file",
    "nifty_lot_regimes", "normalise_units", "oi_to_share_equivalent",
    "reference", "session_boundary_violations", "share_equivalent_to_contracts",
    "share_volume_to_contracts",
    "summarise", "timestamps", "units", "units_verified", "verdicts_by_block",
]
