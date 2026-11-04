"""End-to-end validation smoke test on the cached symbols."""

import pandas as pd

from nifty_quant.research.pipeline import load_market_data, run_research
from nifty_quant.validation import run_validation, save_validation
from nifty_quant.validation.splits import dev_oos_split

SYMBOLS = ["RELIANCE", "TCS", "M_M"]
IDS = ["orders_01", "orders_05", "orders_09", "orders_12", "orders_17", "orders_18"]

data = load_market_data(interval="1d", symbols=SYMBOLS)
results = run_research(interval="1d", strategy_ids=IDS, data=data)

report = run_validation(
    results,
    benchmark=data.benchmark,
    bars=data.bars,
    sensitivity_symbols=SYMBOLS,
)

print("split:", {k: v for k, v in report.split.as_row().items()})
print("\nfolds:", report.metadata["fold_count"])

print("\n--- dev/oos ---")
cols = ["strategy_id", "dev_total_return", "oos_total_return", "dev_sharpe", "oos_sharpe", "oos_return_retention", "symbol_return_correlation"]
print(report.dev_oos[cols].to_string(index=False))

print("\n--- walk-forward summary ---")
print(report.walkforward_summary.to_string(index=False))

print("\n--- robustness ---")
print(report.robustness[["strategy_id", "observations", "psr", "n_trials", "expected_max_sharpe", "deflated_sharpe", "annualised_sharpe", "pct_symbols_positive", "breadth_qvalue"]].to_string(index=False))

print("\n--- sensitivity summary ---")
if report.sensitivity.empty:
    print("(empty)")
else:
    print(report.sensitivity[["strategy_id", "param", "usable_variants", "positive_neighbour_share", "spread", "verdict"]].to_string(index=False))

print("\n--- checks ---")
print(report.checks.pivot_table(index="strategy_id", columns="check", values="passed", aggfunc="first").to_string())

print("\n--- verdicts ---")
print(report.verdicts.to_string(index=False))

print("\n--- regime (equity) head ---")
print(report.regime_equity.head(8).to_string(index=False))

path = save_validation(report, "artifacts/validation_smoke")
print("\nwritten:", sorted(p.name for p in path.iterdir()))
