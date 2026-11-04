import pandas as pd

from nifty_quant.research.pipeline import load_market_data, load_results, run_research, save_results

data = load_market_data(interval="1d", symbols=["RELIANCE", "TCS", "M_M"])
print("audit rows", len(data.audit), "rejected", data.rejected)
print(data.audit[["symbol", "rows", "start", "end"]].to_string(index=False))

results = run_research(
    interval="1d",
    strategy_ids=["orders_01", "orders_05", "orders_09", "orders_12", "orders_17", "orders_18"],
    data=data,
)
print("\nstrategy metrics:")
cols = ["strategy_id", "n_symbols_traded", "n_trades", "total_return", "cagr", "sharpe", "max_drawdown", "median_symbol_sharpe", "pct_symbols_positive", "sufficient_sample"]
print(results.strategy_metrics()[cols].to_string(index=False))

print("\nranking:")
print(results.ranking()[["rank", "strategy_id", "rank_score", "pct_symbols_positive"]].to_string(index=False))

path = save_results(results, tag="smoke", data_audit=data.audit)
print("\nsaved to", path)
print(sorted(p.name for p in path.iterdir()))

reloaded = load_results(path)
print("\nreloaded strategies:", reloaded.strategy_ids)
print(reloaded.strategy_metrics()[["strategy_id", "total_return", "sharpe"]].to_string(index=False))
print("combined equity head:", reloaded.equity_curves().head(2).to_string())
