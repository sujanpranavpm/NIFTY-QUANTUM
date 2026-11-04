"""Round-trip check: save -> load must preserve trade-derived statistics.

Before this check, ``load_results`` attached no trades, so win rate, profit
factor and expectancy silently came back as NaN on a reloaded artifact.
"""

import numpy as np
import pandas as pd

from nifty_quant.research.pipeline import load_market_data, load_results, run_research, save_results

IDS = ["orders_01", "orders_05", "orders_12", "orders_17"]
SYMBOLS = ["RELIANCE", "TCS", "M_M"]

data = load_market_data(interval="1d", symbols=SYMBOLS)
live = run_research(interval="1d", strategy_ids=IDS, data=data)
path = save_results(live, tag="roundtrip", data_audit=data.audit)
reloaded = load_results(path)

cols = ["strategy_id", "n_trades", "win_rate", "profit_factor", "expectancy", "net_realised_pnl"]


def frame(results):
    return results.strategy_metrics()[cols].sort_values("strategy_id").reset_index(drop=True)


left, right = frame(live), frame(reloaded)
print("live:")
print(left.to_string(index=False))
print("\nreloaded:")
print(right.to_string(index=False))

numeric = [c for c in cols if c != "strategy_id"]
diffs = np.abs(left[numeric].to_numpy(float) - right[numeric].to_numpy(float))
print("\nmax abs diff:", np.nanmax(diffs), "| allclose(rtol=1e-6):", np.allclose(
    left[numeric].fillna(-9e99).to_numpy(float),
    right[numeric].fillna(-9e99).to_numpy(float),
    rtol=1e-6,
))

live_trades = live.all_trades()
loaded_trades = reloaded.all_trades()
print("trades live:", len(live_trades), "reloaded:", len(loaded_trades))
print("trade row identical:", live_trades.reset_index(drop=True).equals(loaded_trades.reset_index(drop=True)))
print("equity max diff:", float((live.equity_curves() - reloaded.equity_curves()).abs().max().max()))
