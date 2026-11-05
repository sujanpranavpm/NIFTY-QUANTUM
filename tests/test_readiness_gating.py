"""P2 stage 8 - readiness gating (directive Part 10).

The gate is the thing that authorises P2 ingestion, so it is tested rather than
described.  The single property that matters is *fail-closed*: a resolution is
qualified only when every gate is evidenced by an explicit ``True``.  These tests pin
that the gate cannot be fooled by a partial evidence set, by a truthy-but-not-boolean
value, by a typo, by an un-run evidencing test, or by one resolution having been built
when a different resolution is being qualified.  They also pin the holdout-protection
and resolution-agnostic-walk-forward properties directly.
"""

from __future__ import annotations

import pandas as pd
import pytest

from nifty_quant.validation import (
    GATES,
    RESOLUTIONS,
    assess_readiness,
    generate_folds,
    holdout_protected,
    resolution_dataset_evidenced,
)
from nifty_quant.validation.readiness import (
    MULTIPLE_TESTING_SAFEGUARDED,
    RESOLUTION_DATA_EVIDENCED,
)


def _all_true(resolution: str) -> dict[str, bool]:
    return {g: True for g in GATES} | {RESOLUTION_DATA_EVIDENCED: True, "_resolution": resolution}


# ---------------------------------------------------------------------------
# Fail-closed qualification
# ---------------------------------------------------------------------------
def test_no_evidence_qualifies_nothing():
    for resolution in RESOLUTIONS:
        assessment = assess_readiness(resolution, {})
        assert not assessment.is_qualified
        assert assessment.failed_gates() == list(GATES)         # every gate is unmet
        assert assessment.evidence.get(GATES[0]) is False       # absent != truthy


def test_every_gate_must_be_true_and_one_gap_blocks_the_resolution():
    evidence = _all_true("5m")
    assessment = assess_readiness("5m", evidence)
    assert assessment.is_qualified

    for gate in GATES:
        weakened = dict(evidence)
        weakened[gate] = False                                  # one evidencing check fails
        assert not assess_readiness("5m", weakened).is_qualified
        assert gate in assess_readiness("5m", weakened).failed_gates()


@pytest.mark.parametrize("truthy_not_true", [1, "yes", [0], 2.0, object()])
def test_a_truthy_but_non_boolean_evidence_value_does_not_open_the_gate(truthy_not_true):
    evidence = _all_true("1m")
    evidence[MULTIPLE_TESTING_SAFEGUARDED] = truthy_not_true     # looks passed, is not
    assert assess_readiness("1m", evidence).passed(MULTIPLE_TESTING_SAFEGUARDED) is False
    assert not assess_readiness("1m", evidence).is_qualified


def test_a_mis_spelled_gate_can_never_manufacture_a_qualification():
    evidence = _all_true("15m")
    evidence["contract_identty"] = True                           # typo, ignored on purpose
    assessment = assess_readiness("15m", evidence)
    assert assessment.evidence.get("contract_identty") is None     # not carried in
    assert assessment.is_qualified                                  # only real gates counted


def test_an_unknown_resolution_is_refused():
    with pytest.raises(ValueError, match="unknown resolution"):
        assess_readiness("30m", _all_true("30m"))


# ---------------------------------------------------------------------------
# Per-resolution independence: qualifying one resolution never qualifies another
# ---------------------------------------------------------------------------
def test_a_built_1m_dataset_does_not_imply_the_5m_dataset_exists(tmp_path):
    (tmp_path / "1m__NIFTY__2025-01-30.parquet").write_bytes(b"x")
    assert resolution_dataset_evidenced("1m", tmp_path) is True
    assert resolution_dataset_evidenced("5m", tmp_path) is False    # not inferred from 1m
    assert resolution_dataset_evidenced("15m", tmp_path) is False


def test_qualifying_1m_leaves_5m_unqualified_when_only_1m_data_was_evidenced(tmp_path):
    (tmp_path / "1m__NIFTY__2025-01-30.parquet").write_bytes(b"x")
    shared = {g: True for g in GATES if g != RESOLUTION_DATA_EVIDENCED}

    one_min = assess_readiness("1m", {**shared,
                                      RESOLUTION_DATA_EVIDENCED: resolution_dataset_evidenced("1m", tmp_path)})
    five_min = assess_readiness("5m", {**shared,
                                       RESOLUTION_DATA_EVIDENCED: resolution_dataset_evidenced("5m", tmp_path)})
    assert one_min.is_qualified
    assert not five_min.is_qualified
    assert RESOLUTION_DATA_EVIDENCED in five_min.failed_gates()


def test_no_dataset_directory_evidences_any_resolution(tmp_path):
    for resolution in RESOLUTIONS:
        assert resolution_dataset_evidenced(resolution, tmp_path / "missing") is False


# ---------------------------------------------------------------------------
# Holdout protection
# ---------------------------------------------------------------------------
def test_disjoint_purged_windows_protect_the_holdout():
    _dev_start, dev_end, purge_start, purge_end, oos_start = (
        pd.Timestamp(x) for x in ("2020-01-01", "2022-01-01", "2022-01-03", "2022-01-05", "2022-01-06"))
    assert holdout_protected(dev_end, purge_start, purge_end, oos_start) is True


def test_an_overlapping_or_unpurged_holdout_is_not_protected():
    ts = [pd.Timestamp(x) for x in ("2020-01-01", "2022-06-01", "2022-01-03", "2022-05-01")]
    d0, dev_end, purge_start, oos_start = ts
    assert holdout_protected(dev_end, purge_start, purge_start, oos_start) is False  # dev bleeds past purge
    assert holdout_protected(dev_end, dev_end, purge_start, oos_start) is False      # no purge gap reached holdout


def test_a_missing_purge_band_is_not_protection():
    dev_end = pd.Timestamp("2022-01-01")
    assert holdout_protected(dev_end, None, None, pd.Timestamp("2022-02-01")) is False


# ---------------------------------------------------------------------------
# Walk-forward infrastructure is resolution-agnostic (bar-count, not calendar)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("freq,per_day", [("D", 1), ("min", 75)])   # daily vs 5-minute bars
def test_folds_tile_any_bar_frequency_into_rolling_windows(freq, per_day):
    bars = 3000
    if freq == "min":
        index = pd.date_range("2025-01-01 09:15", periods=bars, freq="5min")
    else:
        index = pd.date_range("2020-01-01", periods=bars, freq="B")
    folds = generate_folds(index, train_bars=1500, test_bars=500)
    assert folds, "a long enough bar index must yield folds at any frequency"
    # test windows tile forward without overlapping the next train start
    for a, b in zip(folds, folds[1:]):
        assert b.test_start > a.test_end
        assert a.train_bars == 1500 and a.test_bars == 500
