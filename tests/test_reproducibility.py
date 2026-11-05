"""P2 stage 7 - reproducibility (directive Part 9).

"Reproducible results" is a qualification gate in Part 10, so it is tested directly here:
two runs that share a configuration must produce an identical equity curve, the config
object must serialise deterministically (no hidden state, no wall-clock bytes), and the
options execution engine must return the identical trade result for the identical bars.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from nifty_quant.config import OPTIMISTIC, ResearchConfig
from nifty_quant.engine.accounting import Portfolio
from nifty_quant.engine.fill_engine import FillEngine
from nifty_quant.engine.options_execution import ExitRules, OptionBar, OptionsExecutionEngine
from nifty_quant.engine.orders import BarData, Order, OrderSide, OrderType


# ---------------------------------------------------------------------------
# Deterministic equity backtest -> identical equity curve
# ---------------------------------------------------------------------------
def _equity_curve() -> list[float]:
    engine = FillEngine(fill_at="open")
    portfolio = Portfolio(initial_capital=100_000.0)
    bars = [BarData(f"b{i}", 100 + i, 101 + i, 99 + i, 100.5 + i, 1000.0) for i in range(10)]
    curve: list[float] = []
    for i, bar in enumerate(bars):
        if i == 0:
            engine.submit(Order(instrument="TEST", side=OrderSide.BUY, quantity=10,
                                order_type=OrderType.MARKET))
        if i == 5:
            engine.submit(Order(instrument="TEST", side=OrderSide.SELL, quantity=10,
                                order_type=OrderType.MARKET))
        for fill in engine.process_bar("TEST", bar):
            portfolio.apply_fill(fill)
        curve.append(portfolio.equity({"TEST": bar.close}))
    return curve


def test_the_same_config_produces_an_identical_equity_curve():
    first = _equity_curve()
    second = _equity_curve()
    assert first == second
    # and it actually moved - an all-constant curve would pass the equality vacuously
    assert max(first) > min(first)


# ---------------------------------------------------------------------------
# Config serialisation is deterministic and loss-free
# ---------------------------------------------------------------------------
def test_research_config_serialises_deterministically(tmp_path):
    a = ResearchConfig()
    b = ResearchConfig()
    assert a.as_dict() == b.as_dict()
    a.to_json(tmp_path / "one.json")
    b.to_json(tmp_path / "two.json")
    assert (tmp_path / "one.json").read_text(encoding="utf-8") == \
        (tmp_path / "two.json").read_text(encoding="utf-8")


def test_config_survives_a_dict_round_trip_unchanged():
    config = ResearchConfig(seed=123456)
    restored = ResearchConfig.from_dict(config.as_dict())
    assert restored == config


# ---------------------------------------------------------------------------
# Options execution determinism
# ---------------------------------------------------------------------------
def _option_bars() -> list[OptionBar]:
    def mk(minutes, o, h, l, c):
        stamp = datetime(2025, 1, 2, 9, 15) + timedelta(minutes=minutes)
        return OptionBar(stamp, o, h, l, c, 1_000_000.0)
    return [mk(0, 100, 101, 99, 100), mk(5, 100, 104, 100, 103),
            mk(10, 105, 112, 104, 110), mk(15, 112, 121, 111, 118)]


def test_options_execution_returns_the_identical_result_for_the_identical_bars():
    engine = OptionsExecutionEngine()
    rules = ExitRules(stop_premium=90.0, target_premium=120.0)
    bars = _option_bars()
    first = engine.run(bars, 0, rules, lots=2, lot_size=75, scenario=OPTIMISTIC)
    second = engine.run(bars, 0, rules, lots=2, lot_size=75, scenario=OPTIMISTIC)
    assert first.net_pnl == second.net_pnl
    assert first.exit_premium == second.exit_premium
    assert first.reason == second.reason
