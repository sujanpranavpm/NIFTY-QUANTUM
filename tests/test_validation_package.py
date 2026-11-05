"""P2 stage 7 - validation-package behaviour (directive Part 9).

Two layers are pinned here.  The *statistical* helpers are exercised on hand-built,
seeded inputs so their numbers are reproducible by eye, and the *report* surface is
exercised both on the committed smoke artifact (via the ``validation_report`` fixture)
and through a synthetic ``save`` -> ``load`` round trip, so a reloaded report shows the
exact frames that were computed rather than re-deriving them.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from nifty_quant.validation import (
    ValidationReport,
    benjamini_hochberg,
    binomial_sign_test,
    bootstrap_mean_ci,
    deflated_sharpe_ratio,
    dev_oos_split,
    expected_max_sharpe,
    load_validation,
    probabilistic_sharpe_ratio,
    save_validation,
)


def _index(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2020-01-01", periods=n, freq="D")


# ---------------------------------------------------------------------------
# Statistical helpers - deterministic and seeded
# ---------------------------------------------------------------------------
def test_benjamini_hochberg_is_monotonic_in_rank_and_bounded():
    pv = [0.001, 0.01, 0.03, 0.20, 0.60]
    q = benjamini_hochberg(pv)
    assert np.nanmin(q) >= 0.0 and np.nanmax(q) <= 1.0
    ranked = q[np.argsort(pv)]                       # ordered smallest-p first
    assert np.all(np.diff(ranked) >= -1e-9)          # adjusted value never decreases by rank


def test_benjamini_hochberg_passes_non_finite_through_as_nan():
    q = benjamini_hochberg([0.01, math.nan, 0.5])
    assert math.isnan(q[1])
    assert np.isfinite(q[0]) and np.isfinite(q[2])


def test_expected_max_sharpe_needs_two_trials_and_grows_with_more():
    assert math.isnan(expected_max_sharpe(1, 0.1))
    assert math.isnan(expected_max_sharpe(10, 0.0))
    hurdle_5 = expected_max_sharpe(5, 0.1)
    hurdle_500 = expected_max_sharpe(500, 0.1)
    assert 0.0 < hurdle_5 < hurdle_500


def test_probabilistic_sharpe_favours_positive_drift_and_needs_three_points():
    rng = np.random.default_rng(1)
    drifting = 0.01 + rng.normal(0.0, 0.01, 300)   # per-period Sharpe near 1.0
    assert probabilistic_sharpe_ratio(drifting) > 0.5
    assert math.isnan(probabilistic_sharpe_ratio([0.01, 0.02]))


def test_bootstrap_mean_ci_is_seeded_and_brackets_the_mean():
    values = [float(v) for v in range(50)]
    first = bootstrap_mean_ci(values)
    second = bootstrap_mean_ci(values)
    assert first == second                            # same seed -> identical interval
    assert first["ci_low"] <= first["mean"] <= first["ci_high"]
    assert math.isnan(bootstrap_mean_ci([1.0])["mean"])   # too few points -> no CI


def test_binomial_sign_test_separates_balanced_from_extreme():
    assert binomial_sign_test(50, 50) > 0.9
    assert binomial_sign_test(90, 10) < 0.05


def test_deflated_sharpe_is_no_larger_than_an_un_hurdled_psr():
    rng = np.random.default_rng(2)
    drifting = 0.002 + rng.normal(0.0, 0.01, 300)
    dsr = deflated_sharpe_ratio(drifting, n_trials=100, sr_std=0.05)
    psr = probabilistic_sharpe_ratio(drifting)
    assert dsr <= psr + 1e-9                          # a higher hurdle lowers the probability


# ---------------------------------------------------------------------------
# Holdout splitting
# ---------------------------------------------------------------------------
def test_dev_oos_split_windows_are_ordered_and_disjoint():
    split = dev_oos_split(_index(500), dev_fraction=0.7, purge_bars=25, min_oos_bars=100)
    assert split.dev_end < split.purge_start
    assert split.purge_end < split.oos_start
    assert split.dev_bars + split.purge_bars + split.oos_bars == 500


def test_dev_oos_split_shrinks_the_embargo_rather_than_returning_a_useless_holdout():
    split = dev_oos_split(_index(200), dev_fraction=0.7, purge_bars=50, min_oos_bars=40)
    assert split.oos_bars >= 40


def test_dev_oos_split_refuses_an_index_too_short_to_hold_out():
    with pytest.raises(ValueError):
        dev_oos_split(_index(10), dev_fraction=0.7, purge_bars=5, min_oos_bars=100)


# ---------------------------------------------------------------------------
# The report surface, on the committed smoke artifact
# ---------------------------------------------------------------------------
def test_report_exposes_every_validation_surface(validation_report):
    frames = validation_report.to_frames()
    assert {"split", "dev_oos", "checks", "verdicts", "walkforward", "robustness"} <= set(frames)
    assert {"strategy_id", "verdict"}.issubset(set(validation_report.verdicts.columns))


def test_grounded_strategies_are_exactly_the_supported_subset(validation_report):
    verdicts = validation_report.verdicts
    supported = set(verdicts.loc[verdicts["verdict"] == "supported", "strategy_id"])
    grounded = validation_report.grounded_strategies()
    assert set(grounded) == supported
    assert grounded == sorted(grounded)


def test_summary_markdown_renders_one_row_per_verdict(validation_report):
    markdown = validation_report.summary_markdown()
    verdicts = validation_report.verdicts
    if verdicts.empty:
        assert markdown == "_no validation results_"
    else:
        assert markdown.splitlines()[0].startswith("| strategy_id")
        assert len(markdown.splitlines()[2:]) == len(verdicts)


# ---------------------------------------------------------------------------
# save -> load round trip (a reloaded report must show what was computed)
# ---------------------------------------------------------------------------
def _synthetic_report() -> ValidationReport:
    split = dev_oos_split(_index(400), dev_fraction=0.7, purge_bars=20, min_oos_bars=100)
    verdicts = pd.DataFrame([{
        "strategy_id": "orders_01", "verdict": "supported",
        "checks_passed": 10, "checks_failed": 0, "failed_checks": "",
    }])
    checks = pd.DataFrame([{"strategy_id": "orders_01", "check": "min_trades", "passed": True}])
    empty = pd.DataFrame
    return ValidationReport(
        split=split, dev_oos=empty(), walkforward=empty(), walkforward_summary=empty(),
        sensitivity=empty(), sensitivity_detail=empty(), regime_equity=empty(),
        regime_trades=empty(), consistency=empty(), robustness=empty(),
        checks=checks, verdicts=verdicts, metadata={"thresholds": {"min_trades": 10}})


def test_save_and_load_round_trips_the_computed_frames(tmp_path):
    report = _synthetic_report()
    directory = tmp_path / "validation"
    save_validation(report, directory)
    loaded = load_validation(directory)

    assert loaded.verdicts.loc[0, "strategy_id"] == "orders_01"
    assert loaded.grounded_strategies() == ["orders_01"]
    assert loaded.split.dev_bars == report.split.dev_bars
    assert loaded.metadata["reloaded_from"]           # provenance stamped on reload


def test_load_validation_refuses_a_directory_that_is_not_one(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_validation(tmp_path / "does_not_exist")
