"""Print the exact cells that differ between artifacts/smoke and a fresh run."""

import pandas as pd

from nifty_quant.research.pipeline import load_market_data, load_results, run_research

IDS = ["orders_01", "orders_05", "orders_09", "orders_12", "orders_17", "orders_18"]
SYMBOLS = ["RELIANCE", "TCS", "M_M"]

baseline = load_results("artifacts/smoke")
data = load_market_data(interval="1d", symbols=SYMBOLS)
fresh = run_research(interval="1d", strategy_ids=IDS, data=data)


def diff(label, old, new):
    print(f"\n=== {label} ===")
    old_al, new_al = old.align(new, join="outer")
    mask = ~((old_al == new_al) | (old_al.isna() & new_al.isna()))
    if not mask.any().any():
        print("identical")
        return
    for col in old.columns:
        bad = mask[col]
        if bad.any():
            print(f"-- {col}")
            frame = pd.DataFrame({"old": old_al.loc[bad, col], "new": new_al.loc[bad, col]})
            frame["delta"] = frame["new"] - frame["old"]
            print(frame.to_string())


diff(
    "strategy_metrics",
    baseline.strategy_metrics().set_index("strategy_id"),
    fresh.strategy_metrics().set_index("strategy_id"),
)
diff(
    "symbol_metrics",
    baseline.symbol_metrics().set_index(["strategy_id", "symbol"]),
    fresh.symbol_metrics().set_index(["strategy_id", "symbol"]),
)
