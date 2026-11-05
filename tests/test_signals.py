"""Tests for the verdict-gated signal engine.

These tests cover the gating logic, signal parsing, integrity constraints and
reproducibility. They do not assert that any strategy is profitable; they
assert that the engine truthfully reports what the validated strategy is
expressing at the close of the last bar.
"""

from __future__ import annotations

import pandas as pd
import pytest

from nifty_quant.signals import SignalEngine
from nifty_quant.signals.schemas import SignalAction
from nifty_quant.validation.report import ValidationReport

SYMBOLS = ["RELIANCE", "TCS", "M_M"]


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------
def test_missing_validation_raises():
    with pytest.raises(ValueError, match="validation report"):
        SignalEngine(None)  # type: ignore[arg-type]

    empty = ValidationReport(
        split=None,  # type: ignore[arg-type]
        dev_oos=pd.DataFrame(),
        walkforward=pd.DataFrame(),
        walkforward_summary=pd.DataFrame(),
        sensitivity=pd.DataFrame(),
        sensitivity_detail=pd.DataFrame(),
        regime_equity=pd.DataFrame(),
        regime_trades=pd.DataFrame(),
        consistency=pd.DataFrame(),
        robustness=pd.DataFrame(),
        checks=pd.DataFrame(),
        verdicts=pd.DataFrame(),
    )
    with pytest.raises(ValueError, match="validation report"):
        SignalEngine(empty)


def test_rejected_strategies_are_blocked(engine, market_data):
    signals = engine.generate(market_data.bars, symbols=SYMBOLS)
    assert signals
    assert all(s.action == SignalAction.BLOCKED for s in signals)
    assert all(not s.valid for s in signals)
    assert all(
        any(phrase in s.validity_reason.lower() for phrase in ("verdict", "did not pass", "critical check", "restart disagreed"))
        for s in signals
    )


def test_approved_strategy_generates_valid_signals(approved_engine, market_data):
    signals = approved_engine.generate(market_data.bars, symbols=SYMBOLS)
    orders_01 = [s for s in signals if s.strategy_id == "orders_01"]
    others = [s for s in signals if s.strategy_id != "orders_01"]

    assert len(orders_01) == len(SYMBOLS)
    assert all(s.valid for s in orders_01)
    assert all(s.verdict == "supported" for s in orders_01)
    assert all(s.action != SignalAction.BLOCKED for s in orders_01)
    assert all(s.action in {SignalAction.BUY, SignalAction.HOLD, SignalAction.FLAT, SignalAction.EXIT} for s in orders_01)

    assert others
    assert all(s.action == SignalAction.BLOCKED for s in others)


def test_strict_split_failure_blocks(market_data, validation_report):
    """A strategy whose restarted holdout disagreed with the sliced run is denied."""
    from dataclasses import replace

    report = replace(
        validation_report,
        verdicts=validation_report.verdicts.copy(),
        strict_split=validation_report.strict_split.copy(),
    )
    # Pretend orders_01 passed every check but the restart disagree.
    report.verdicts.loc[report.verdicts["strategy_id"] == "orders_01", "verdict"] = "supported"
    report.checks.loc[report.checks["strategy_id"] == "orders_01", "passed"] = True
    if not report.strict_split.empty:
        report.strict_split.loc[
            report.strict_split["strategy_id"] == "orders_01", "ok"
        ] = False

    engine = SignalEngine(report)
    signals = engine.generate(market_data.bars, symbols=SYMBOLS)
    orders_01 = [s for s in signals if s.strategy_id == "orders_01"]
    assert orders_01
    assert all(not s.valid for s in orders_01)
    assert all("restart disagreed" in s.validity_reason for s in orders_01)


# ---------------------------------------------------------------------------
# Integrity
# ---------------------------------------------------------------------------
def test_no_duplicate_signals(approved_engine, market_data):
    signals = approved_engine.generate(market_data.bars, symbols=SYMBOLS)
    keys = [(s.strategy_id, s.symbol) for s in signals]
    assert len(keys) == len(set(keys))


def test_missing_market_data_yields_no_signals(engine):
    assert engine.generate({}) == []
    assert engine.generate({"UNKNOWN": pd.DataFrame()}) == []


def test_no_look_ahead_and_no_future_leak(approved_engine, market_data):
    signals = approved_engine.generate(market_data.bars, symbols=SYMBOLS)
    latest = max(
        pd.DatetimeIndex(frame.index).max()
        for frame in market_data.bars.values()
    )
    for signal in signals:
        if signal.as_of is not None:
            assert signal.as_of <= latest, f"{signal.strategy_id}/{signal.symbol} looks ahead"
        if signal.signal_at is not None:
            assert signal.signal_at <= latest
        if signal.reference_price is not None:
            # The reference price is the last close, which is <= latest close.
            last_close = market_data.bars[signal.symbol].loc[:latest, "close"].iloc[-1]
            assert signal.reference_price == pytest.approx(float(last_close), rel=1e-6)


def test_earliest_fill_after_signal(approved_engine, market_data):
    signals = approved_engine.generate(market_data.bars, symbols=SYMBOLS)
    for signal in signals:
        if signal.valid and signal.action in (SignalAction.BUY, SignalAction.SELL, SignalAction.EXIT):
            assert signal.earliest_fill_at is not None
            assert signal.earliest_fill_at > signal.as_of


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def test_bracket_stop_target_and_risk_reward(bracket_signal, bracket_live_signal):
    """orders_12's +6%/-3% bracket resolves to a 2:1 R/R - on a deterministic fixture.

    This used to be skipped whenever the cached equity data happened to have no
    fresh orders_12 BUY at the end of the sample, so its 2:1 assertion had never
    actually run.  The ``bracket_*`` fixtures force exactly one golden cross on a
    chosen bar, so both stages of the bracket are now observed rather than hoped for:

    * on the cross bar the entry is still pending, so no exit child has activated yet;
    * one bar later the entry has filled and both the +6% take-profit and the -3%
      stop-loss are live, giving the published 2:1 reward/risk.
    """
    from conftest import BRACKET_CROSS_CLOSE

    # Stage 1: a fresh entry that cannot fill before the next bar.
    assert bracket_signal.action == SignalAction.BUY
    assert bracket_signal.position == 0.0
    assert bracket_signal.entry_price == pytest.approx(BRACKET_CROSS_CLOSE)
    assert bracket_signal.earliest_fill_at > bracket_signal.as_of
    # The bracket children only activate on the first fill, so nothing is exitable yet.
    assert bracket_signal.target is None and bracket_signal.stop_loss is None

    # Stage 2: the entry has filled, so both legs of the bracket are now readable.
    assert bracket_live_signal.action == SignalAction.EXIT
    assert bracket_live_signal.position > 0.0
    assert bracket_live_signal.entry_price is not None
    assert bracket_live_signal.stop_loss is not None
    assert bracket_live_signal.target is not None
    assert bracket_live_signal.risk_reward == pytest.approx(2.0, rel=0.05)
    assert any("stop" in cond.lower() for cond in bracket_live_signal.exit_conditions)
    assert any("target" in cond.lower() for cond in bracket_live_signal.exit_conditions)

    # The bracket was built at +6%/-3% from the signal-bar close.
    entry = bracket_live_signal.entry_price
    assert bracket_live_signal.target == pytest.approx(round(entry * 1.06, 2))
    assert bracket_live_signal.stop_loss == pytest.approx(round(entry * 0.97, 2), abs=0.01)


def test_signal_fingerprint_reproducibility(approved_engine, market_data):
    """The same inputs must produce the same deterministic fingerprint."""
    signals_a = approved_engine.generate(market_data.bars, symbols=SYMBOLS)
    signals_b = approved_engine.generate(market_data.bars, symbols=SYMBOLS)
    by_key_a = {(s.strategy_id, s.symbol): s for s in signals_a}
    by_key_b = {(s.strategy_id, s.symbol): s for s in signals_b}
    assert by_key_a.keys() == by_key_b.keys()
    for key in by_key_a:
        assert by_key_a[key].fingerprint == by_key_b[key].fingerprint
        assert by_key_a[key].action == by_key_b[key].action
        assert by_key_a[key].entry_price == pytest.approx(by_key_b[key].entry_price, abs=1e-9) if by_key_a[key].entry_price is not None else by_key_b[key].entry_price is None


# ---------------------------------------------------------------------------
# Export integration
# ---------------------------------------------------------------------------
def test_signals_export_round_trip(tmp_path, approved_engine, market_data):
    from nifty_quant.signals.io import save_signals, signals_by_strategy

    signals = approved_engine.generate(market_data.bars, symbols=SYMBOLS)
    path = save_signals(signals, tmp_path)
    assert path.exists()
    loaded = pd.read_csv(path)
    assert len(loaded) == len(signals)
    assert set(loaded["strategy_id"]) == {s.strategy_id for s in signals}
    assert signals_by_strategy(signals)
