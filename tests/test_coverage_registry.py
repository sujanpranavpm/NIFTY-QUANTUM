"""Stage 3 tests: the persistent coverage registry and strict timestamp validation.

Synthetic cells (hand-countable) verify the mapping, the guards and the integrity
measures without any dataset dependency; a second, opt-in layer loads the built
``data/reference/nifty_coverage_registry.csv`` and proves it reconciles to the
completeness report and carries no silently-skipped contract.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from nifty_quant.derivatives.coverage_registry import (
    CONTRACT_DAY_LEVEL,
    EXPIRY_LEVEL,
    MISSING_CONTRACT,
    MISSING_FILE,
    PRESENT,
    PUBLISHED_NOT_CACHED,
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
from nifty_quant.derivatives.reference import REFERENCE_DIR
from nifty_quant.derivatives.timestamps import (
    duplicate_timestamps,
    is_closing_print,
    session_boundary_violations,
)

BUILT = REFERENCE_DIR / "nifty_coverage_registry.csv"
M5_JSON = Path("logs/m5_completeness.json")


# ---------------------------------------------------------------------------
# completeness -> registry vocabulary mapping
# ---------------------------------------------------------------------------
def test_day_verdicts_map_to_the_directive_vocabulary():
    assert registry_status_for_day("PRESENT") == PRESENT
    assert registry_status_for_day("NO_TRADES") == "NO_TRADE"
    assert registry_status_for_day("MISSING_DAYS") == MISSING_CONTRACT
    assert registry_status_for_day("MISSING_TRADES") == "MISSING_CANDLE"
    assert registry_status_for_day("TRUNCATED_SESSION") == TRUNCATED_SESSION
    assert registry_status_for_day("INACTIVE") == "INACTIVE_CONTRACT"
    assert registry_status_for_day("PRESENT_WITHOUT_OI") == "PRESENT_WITHOUT_OI"


def test_file_verdicts_map_and_unknown_raises():
    assert registry_status_for_file("PRESENT") == PRESENT
    assert registry_status_for_file("MISSING_FILE") == MISSING_FILE
    assert registry_status_for_file("TRUNCATED_TAIL") == TRUNCATED_TAIL
    with pytest.raises(ValueError, match="no registry mapping"):
        registry_status_for_day("NOT_A_STATUS")


# ---------------------------------------------------------------------------
# high/low integrity: only compares where an official range exists
# ---------------------------------------------------------------------------
def test_high_low_flag_is_none_without_official_evidence():
    assert high_low_within_official(10.0, 9.0, None, None) is None
    assert high_low_within_official(float("nan"), 9.0, 10.0, 9.0) is None


def test_high_low_flag_true_inside_and_false_beyond_tolerance():
    assert high_low_within_official(10.0, 9.0, 10.05, 8.95) is True   # inside range
    assert high_low_within_official(10.5, 9.0, 10.0, 8.9) is False     # 0.5 above high


# ---------------------------------------------------------------------------
# candle gaps are detected inside the contract's own span, never fabricated
# ---------------------------------------------------------------------------
def test_missing_candle_minutes_counts_in_span_gaps():
    labels = ["09:15", "09:16", "09:17", "09:20"]     # 09:18, 09:19 missing
    assert missing_candle_minutes(labels) == 2


def test_missing_candle_minutes_is_zero_for_continuous_or_single():
    assert missing_candle_minutes(["09:15", "09:16", "09:17"]) == 0
    assert missing_candle_minutes(["09:15"]) == 0
    assert missing_candle_minutes([]) == 0


# ---------------------------------------------------------------------------
# timestamp strict validation
# ---------------------------------------------------------------------------
def _ts(labels, strike=100, ot="CE"):
    return pd.DataFrame({
        "timestamp": pd.to_datetime(["2024-01-02 " + x for x in labels]),
        "strike": strike, "option_type": ot})


def test_duplicate_timestamps_are_surfaced_not_dropped():
    frame = _ts(["09:15", "09:16", "09:16"])     # one duplicated minute
    dups = duplicate_timestamps(frame)
    assert len(dups) == 2
    assert set(dups["timestamp"].dt.strftime("%H:%M")) == {"09:16"}


def test_session_boundary_violations_flags_stray_labels():
    assert session_boundary_violations(["09:15", "12:00", "15:30"]) == ()
    assert session_boundary_violations(["09:14", "15:54"]) == ("09:14", "15:54")


def test_closing_print_is_only_the_15_30_bucket():
    assert is_closing_print(pd.Timestamp("2024-01-02 15:30")) is True
    assert is_closing_print(pd.Timestamp("2024-01-02 15:29")) is False


# ---------------------------------------------------------------------------
# registry container: counts, persistence round-trip, silent-skip guard
# ---------------------------------------------------------------------------
def cell(day, status, *, expected=True, observed=False, reason="x", arbiter=True):
    return CoverageEntry(level=CONTRACT_DAY_LEVEL, trading_day=day, expiry="2024-02-01",
                         strike=100, option_type="CE", status=status, expected=expected,
                         observed=observed, arbiter=arbiter, reason=reason)


def test_registry_counts_are_per_level():
    reg = CoverageRegistry([
        cell("2024-01-02", PRESENT, observed=True),
        cell("2024-01-03", MISSING_CONTRACT),
        CoverageEntry(level=EXPIRY_LEVEL, expiry="2024-02-01", status=MISSING_FILE)])
    assert reg.counts(CONTRACT_DAY_LEVEL) == {PRESENT: 1, MISSING_CONTRACT: 1}
    assert reg.counts(EXPIRY_LEVEL) == {MISSING_FILE: 1}


def test_silent_skip_guard_raises_when_a_missing_contract_has_no_reason():
    reg = CoverageRegistry([cell("2024-01-03", MISSING_CONTRACT, reason="")])
    with pytest.raises(AssertionError, match="classified missing with no reason"):
        reg.assert_no_silent_skip()
    # the same cell with a reason is not a silent skip
    ok = CoverageRegistry([cell("2024-01-03", MISSING_CONTRACT,
                                reason="exchange traded, no provider bars")])
    ok.assert_no_silent_skip()


def test_registry_csv_round_trip_preserves_the_tri_state_flag(tmp_path):
    reg = CoverageRegistry([
        CoverageEntry(level=CONTRACT_DAY_LEVEL, trading_day="2024-01-02",
                      expiry="2024-02-01", strike=100, option_type="CE",
                      status=PRESENT, observed=True, high_low_flag=True),
        CoverageEntry(level=CONTRACT_DAY_LEVEL, trading_day="2024-01-03",
                      expiry="2024-02-01", strike=100, option_type="CE",
                      status=PRESENT, observed=True, high_low_flag=False),
        CoverageEntry(level=CONTRACT_DAY_LEVEL, trading_day="2024-01-04",
                      expiry="2024-02-01", strike=100, option_type="CE",
                      status=PRESENT, observed=True, high_low_flag=None)])
    path = tmp_path / "reg.csv"
    reg.write_csv(path)
    again = CoverageRegistry.from_csv(path)
    got = {e.high_low_flag for e in again.entries}
    assert got == {True, False, None}      # tri-state survives the CSV round-trip
    assert len(again) == 3


def test_max_observed_coverage_counts_only_arbitrated_cells():
    reg = CoverageRegistry([
        cell("2024-01-02", PRESENT, observed=True, arbiter=True),
        cell("2024-01-03", MISSING_CONTRACT, arbiter=True),
        cell("2024-01-04", UNKNOWN, arbiter=False)])          # excluded from the ceiling
    cov = reg.max_observed_coverage()
    assert cov["arbitrated_contract_days"] == 2
    assert cov["present_share"] == 50.0


def test_reconcile_detects_drift():
    reg = CoverageRegistry([cell("2024-01-02", PRESENT, observed=True)])
    assert reg.reconcile({PRESENT: 1}, CONTRACT_DAY_LEVEL)["ok"] is True
    bad = reg.reconcile({PRESENT: 2}, CONTRACT_DAY_LEVEL)
    assert bad["ok"] is False
    assert bad["differences"][PRESENT] == {"registry": 1, "report": 2}


# ---------------------------------------------------------------------------
# the built registry (opt-in, runs after scripts/_p2_coverage_registry.py)
# ---------------------------------------------------------------------------
needs_built = pytest.mark.skipif(not BUILT.exists(),
                                 reason="registry not built (_p2_coverage_registry.py)")


@needs_built
def test_built_registry_has_both_levels_and_no_silent_skip():
    reg = CoverageRegistry.from_csv(BUILT)
    levels = {e.level for e in reg.entries}
    assert {EXPIRY_LEVEL, CONTRACT_DAY_LEVEL} <= levels
    reg.assert_no_silent_skip()


@needs_built
def test_built_registry_reconciles_to_the_completeness_report():
    if not M5_JSON.exists():
        pytest.skip("no completeness report to reconcile against")
    reg = CoverageRegistry.from_csv(BUILT)
    # the completeness report enumerates only exchange-listed cells, so reconcile that
    # subset; the registry additionally carries provider-only and aggregated rows.
    counts = reg.counts(CONTRACT_DAY_LEVEL, expected=True)
    import json
    m5 = json.loads(M5_JSON.read_text(encoding="utf-8"))
    m5_mapped = {registry_status_for_day(k): v
                 for k, v in m5.get("day_counts", {}).items()}
    assert sum(counts.values()) == sum(m5_mapped.values())
    for status, value in m5_mapped.items():
        assert counts.get(status, 0) == value, f"{status} drifted"


@needs_built
def test_unknown_cells_have_no_arbiter_and_carry_a_reason():
    reg = CoverageRegistry.from_csv(BUILT)
    unknowns = [e for e in reg.entries if e.status == UNKNOWN
                and e.level == CONTRACT_DAY_LEVEL]
    assert unknowns, "the 2021-2023 provider days must appear as UNKNOWN cells"
    for e in unknowns:
        assert e.arbiter is False                       # unknown means "no arbiter"
        assert e.reason.strip()                         # never a blank classification
