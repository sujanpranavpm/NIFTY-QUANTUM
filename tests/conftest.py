"""Shared fixtures for the test suite.

The fixtures intentionally reuse the smoke artifacts rather than fetching new
network data, so the tests are deterministic and offline once the artifacts
exist.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from nifty_quant.research.pipeline import load_market_data
from nifty_quant.signals import SignalEngine
from nifty_quant.validation import load_validation

SYMBOLS = ["RELIANCE", "TCS", "M_M"]


@pytest.fixture(scope="module")
def market_data():
    """Cached OHLCV bars for the smoke universe."""
    return load_market_data(interval="1d", symbols=SYMBOLS)


@pytest.fixture(scope="module")
def validation_report():
    """The validation report produced by the smoke run."""
    return load_validation("artifacts/validation_smoke")


@pytest.fixture
def approved_validation_report(validation_report):
    """A copy of the smoke report with ``orders_01`` marked supported.

    This lets the signal-engine tests exercise the approved path without
    pretending that any real strategy is actually profitable.
    """
    report = replace(
        validation_report,
        verdicts=validation_report.verdicts.copy(),
        checks=validation_report.checks.copy(),
    )
    mask = report.verdicts["strategy_id"] == "orders_01"
    report.verdicts.loc[mask, "verdict"] = "supported"
    report.verdicts.loc[mask, "checks_passed"] = 10
    report.verdicts.loc[mask, "checks_failed"] = 0
    report.verdicts.loc[mask, "critical_failures"] = 0
    report.verdicts.loc[mask, "failed_checks"] = ""
    report.checks.loc[report.checks["strategy_id"] == "orders_01", "passed"] = True
    return report


@pytest.fixture
def engine(validation_report):
    return SignalEngine(validation_report)


@pytest.fixture
def approved_engine(approved_validation_report):
    return SignalEngine(approved_validation_report)
