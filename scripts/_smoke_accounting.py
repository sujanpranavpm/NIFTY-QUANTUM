import pandas as pd

from nifty_quant.engine.backtest import run_backtest

bars = {s: pd.read_parquet(f"data/cache/1d/{s}.parquet") for s in ["RELIANCE", "TCS", "M_M"]}
run = run_backtest("orders_01", bars)

for sl in run.slices:
    eq = sl.equity["equity"]
    pnl = sl.trades["net_pnl"].sum() if not sl.trades.empty else 0.0
    print(
        f"{sl.label}: equity {eq.iloc[0]:.2f} -> {eq.iloc[-1]:.2f} "
        f"(delta {eq.iloc[-1] - eq.iloc[0]:.2f}) | trade net {pnl:.2f} "
        f"| fees {sl.metrics['fees_paid']:.2f} | slip {sl.metrics['slippage_paid']:.2f} "
        f"| open {sl.diagnostics['open_positions_at_end']}"
    )

sl = run.slices[0]
print("\nfills sample:")
print(sl.fills.head(4).to_string())
print("\ntrades sample:")
cols = ["entry_time", "exit_time", "entry_price", "exit_price", "quantity", "gross_pnl", "fees", "net_pnl", "exit_reason"]
print(sl.trades[cols].head(6).to_string())
print("\nfee breakdown of one fill:")
print(sl.fills.iloc[0]["fees"])
