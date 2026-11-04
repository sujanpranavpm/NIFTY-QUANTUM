"""Shared fixtures for the test suite.

Two kinds of fixture live here:

* the smoke-artifact loaders the signal tests have always used, and
* ``bar``/``bars``/``order``/``clock`` and ``minute_block``/``official_eod`` -
  deterministic, hand-built bars and frames for the core-execution and data-normalisation
  tests, which must not depend on any downloaded dataset.

Everything is offline and deterministic: no fixture touches the network, ``data/`` or
``artifacts/``.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

# There is no installed package and no pyproject.toml, so a bare ``pytest`` run (unlike
# ``python -m pytest``) does not put the repo root on sys.path. Pin it explicitly so both
# invocations behave identically.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nifty_quant.research.pipeline import load_market_data
from nifty_quant.engine.orders import BarData, Order, OrderSide, OrderType
from nifty_quant.signals import SignalEngine
from nifty_quant.validation import load_validation

SYMBOLS = ["RELIANCE", "TCS", "M_M"]


@pytest.fixture(scope="module")
def market_data():
    """Cached OHLCV bars for the smoke universe."""
    return load_market_data(interval="1d", symbols=SYMBOLS)


@pytest.fixture(scope="module")
def validation_report():
    """The validation report produced by the smoke run.

    Root-anchored rather than CWD-relative, so the suite passes from the repo root
    and from ``tests/`` alike.
    """
    return load_validation(str(ROOT / "artifacts" / "validation_smoke"))


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


# ---------------------------------------------------------------------------
# Deterministic core-execution fixtures
#
# These exist so that the fill/cost/accounting tests never touch a downloaded
# dataset: every price, quantity and timestamp is written out literally in the
# test that uses it, or derived from a rule the test restates.
# ---------------------------------------------------------------------------
@pytest.fixture
def bar():
    """``bar(ts, o, h, l, c, v)`` -> BarData with a plain-string timestamp."""
    def make(ts: str, o: float, h: float, l: float, c: float, v: float = 1_000.0) -> BarData:
        return BarData(timestamp=ts, open=o, high=h, low=l, close=c, volume=v)
    return make


@pytest.fixture
def bars(bar):
    """``bars(("09:15", 100, 101, 99, 100), ("09:30", 100, 102, 99, 101, 500))`` -> list[BarData]."""
    def make(*rows: tuple) -> list[BarData]:
        return [bar(*row) for row in rows]
    return make


@pytest.fixture
def order():
    """``order(side, qty, type, **fields)`` -> Order on instrument ``TEST``."""
    def make(side: OrderSide = OrderSide.BUY,
             quantity: float = 10.0,
             order_type: OrderType = OrderType.MARKET,
             instrument: str = "TEST",
             **fields) -> Order:
        return Order(instrument=instrument, side=side, quantity=quantity,
                     order_type=order_type, **fields)
    return make


@pytest.fixture
def clock():
    """Deterministic session timestamps, so tests never depend on the machine clock."""
    return [f"2026-01-05 {h:02d}:{m:02d}:00" for h in (9, 10) for m in range(0, 60, 15)]


# ---------------------------------------------------------------------------
# Deterministic derivatives-data fixtures
#
# Same rule as above: every value is written out literally or derived from a rule
# the test restates, no frame here is read from data/ or artifacts/.
# ---------------------------------------------------------------------------
IST = "Asia/Kolkata"
DEFAULT_DAY = "2026-01-05"
DEFAULT_EXPIRY = "2026-01-29"


def minute_labels(count: int, start_index: int = 0) -> list[str]:
    """``HH:MM`` bucket labels for ``count`` consecutive minutes from the session open.

    ``minute_labels(375)`` ends at 15:29, the last in-session bucket; adding one more
    produces the 15:30 closing-print bucket.
    """
    base = 9 * 60 + 15 + start_index
    return [f"{(base + i) // 60:02d}:{(base + i) % 60:02d}" for i in range(count)]


@pytest.fixture
def minute_block():
    """Build one (trading day x expiry) block of one-minute buckets.

    ``minute_block(labels=[...], volume_by_label={"09:15": 500.0, "15:30": 4.0},
    degenerate_labels=("15:30",))``.  Prices rise one rupee per bucket and open interest
    rises by the same amount, so any expected OHLC, volume or last-OI value can be
    written out by hand.  ``volume_by_label`` is per *row*, so the block total for a
    bucket is that value times the number of contracts - the ratios the session
    classifier measures are unchanged by the contract count.
    """
    def make(*, labels=None, volume_by_label=None,
             contracts=(("CE", 24000), ("PE", 24000)),
             symbol="NIFTY", expiry=DEFAULT_EXPIRY, day=DEFAULT_DAY,
             base_price=100.0, oi_base=1000.0, degenerate_labels=(), tz=True):
        labels = list(minute_labels(25) if labels is None else labels)
        per_label = dict(volume_by_label or {})
        rows = []
        for option_type, strike in contracts:
            for i, label in enumerate(labels):
                price = base_price + i
                single = label in degenerate_labels
                stamp = pd.Timestamp(f"{day} {label}:00")
                if tz:
                    stamp = stamp.tz_localize(IST)
                rows.append({
                    "symbol": symbol, "trading_day": day, "expiry": expiry,
                    "strike": int(strike), "option_type": option_type,
                    "timestamp": stamp,
                    "open": price, "high": price if single else price + 0.5,
                    "low": price if single else price - 0.5, "close": price,
                    "volume": float(per_label.get(label, 100.0)),
                    "open_interest": oi_base + i,
                })
        return pd.DataFrame(rows)
    return make


@pytest.fixture
def official_eod():
    """``official_eod(rows)`` -> a one-row-per-contract-per-day EOD frame.

    ``rows`` are ``(trading_day, expiry, strike, option_type, open_interest)`` tuples in
    the mixed representation the two sources actually use, so the join's normalisation is
    exercised rather than assumed.
    """
    def make(rows) -> pd.DataFrame:
        return pd.DataFrame(
            [{"trading_day": day, "expiry": expiry, "strike": strike,
              "option_type": opt, "OpnIntrst": oi}
             for day, expiry, strike, opt, oi in rows])
    return make
