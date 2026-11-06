"""P2.2.6.1 addendum: independent 5m sensitivity recompute (read-only)."""
import pandas as pd
import _p2261_reconcile as R

for label, path in (("as-run +-30 5m", "artifacts/p225/5m/trades.csv"),
                    ("approved 5m", "artifacts/p226/5m/trades.csv")):
    df = pd.read_csv(path)
    for sid in R.TIER1:
        sub = df[df.strategy_id == sid]
        m = R.indep_metrics(R.dv(sub[sub.scenario == R.BASE]))
        ue = R.unique_entries(sub)
        print(f"{label} {sid}: dev={ue['Development']} n={m['n_pnl_trades']} "
              f"exp_net={m['expectancy_net']:.1f} exp_ret={m['expectancy_ret']:.5f} "
              f"PF={m['profit_factor']:.3f} dd={m['max_drawdown_chrono']:.5f}")
