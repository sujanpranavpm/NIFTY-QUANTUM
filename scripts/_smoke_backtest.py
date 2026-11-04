import pandas as pd

from nifty_quant.engine.backtest import run_backtest

bars = {s: pd.read_parquet(f"data/cache/1d/{s}.parquet") for s in ["RELIANCE", "TCS", "M_M"]}
for sid in ["orders_01", "orders_03", "orders_05", "orders_07", "orders_12", "orders_13", "orders_18"]:
    run = run_backtest(sid, bars)
    bad = n = same_bar = 0
    for sl in run.slices:
        if sl.orders.empty or sl.fills.empty:
            continue
        if "order_id" not in sl.orders.columns:
            print(sid, sl.label, "orders columns:", list(sl.orders.columns))
            continue
        created = sl.orders.set_index("order_id")["created_at"]
        lag = sl.fills["timestamp"] - sl.fills["order_id"].map(created)
        n += len(lag)
        bad += int((lag <= pd.Timedelta(0)).sum())
        same_bar += int((lag < pd.Timedelta(days=1)).sum())
    print(sid, "fills", n, "violations", bad, "same-day", same_bar)
