"""A/B check: the numpy panel path must be bit-identical to the pandas path.

Both arms run through the *same* runner; only the panel/bar access layer is
swapped. ``fast`` uses ``engine.panel.PanelView`` + ``_SymbolBars``; ``slow``
injects the original pandas-backed implementations.
"""

import pandas as pd

from nifty_quant.engine import backtest
from nifty_quant.engine.features import trim_panels
from nifty_quant.engine.orders import BarData
from nifty_quant.research.pipeline import load_market_data, run_research

IDS = ["orders_01", "orders_03", "orders_05", "orders_09", "orders_12", "orders_14",
       "orders_16", "orders_17", "orders_18"]
SYMBOLS = ["RELIANCE", "TCS", "M_M"]

FAST_TO_VIEWS = backtest.to_views
FAST_BARS = backtest._SymbolBars


class PandasBarSource:
    """Pre-rewrite behaviour: resolve each bar with ``frame.loc[timestamp]``."""

    def __init__(self, frame, rows=None):
        self.frame = frame

    def bar_at(self, timestamp):
        if timestamp not in self.frame.index:
            return None
        row = self.frame.loc[timestamp]
        close = float(row["close"])
        if not (close == close) or close <= 0:
            return None
        return BarData(
            timestamp=timestamp,
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=close,
            volume=float(row.get("volume", 0.0) or 0.0),
        )


def run(arm):
    if arm == "slow":
        backtest.to_views = lambda panels, index, rows=None: trim_panels(panels, index)
        backtest._SymbolBars = PandasBarSource
    else:
        backtest.to_views = FAST_TO_VIEWS
        backtest._SymbolBars = FAST_BARS
    data = load_market_data(interval="1d", symbols=SYMBOLS)
    return run_research(interval="1d", strategy_ids=IDS, data=data)


def compare(name, left, right):
    try:
        pd.testing.assert_frame_equal(left, right, check_exact=True, check_like=True)
        print(f"  OK   {name}  shape={left.shape}")
        return 0
    except AssertionError as exc:
        print(f"  FAIL {name}: {str(exc).splitlines()[0]}")
        for line in str(exc).splitlines()[1:5]:
            print(f"       {line}")
        return 1


fast = run("fast")
slow = run("slow")

print("\n--- comparing fast vs pandas baseline ---")
problems = 0
problems += compare(
    "strategy_metrics",
    slow.strategy_metrics().sort_values("strategy_id").reset_index(drop=True),
    fast.strategy_metrics().sort_values("strategy_id").reset_index(drop=True),
)
problems += compare(
    "symbol_metrics",
    slow.symbol_metrics().sort_values(["strategy_id", "symbol"]).reset_index(drop=True),
    fast.symbol_metrics().sort_values(["strategy_id", "symbol"]).reset_index(drop=True),
)
for sid in IDS:
    old_trades = slow.runs[sid].trades()
    new_trades = fast.runs[sid].trades()
    if old_trades.empty and new_trades.empty:
        print(f"  OK   trades[{sid}] both empty")
        continue
    problems += compare(f"trades[{sid}]", old_trades, new_trades)
problems += compare("equity_curves", slow.equity_curves(), fast.equity_curves())
problems += compare("monthly_returns", slow.monthly_return_panel(), fast.monthly_return_panel())

print("\ndifferences:", problems)
