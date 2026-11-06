"""P2.2.3 adapter runner: the five Tier-1 strategies, 15m primary + 5m sensitivity.

Persists per-trade and per-rejection records under ``artifacts/p223`` and prints a
compact summary.  This is a *reproducibility* helper only - it changes no strategy,
optimises nothing, and declares no winner.

    $env:PYTHONPATH='.'; python scripts/_p223_run_adapter.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from nifty_quant.options.pipeline import TIER1, AdapterConfig, ExitConvention, OptionTradeAdapter

ARTIFACTS = Path("artifacts") / "p223"

# P2.2.6.2 pin: this experiment ran under the then-default +/-30%/30% band.  The
# library default is now the approved +15%/-8% policy, so the historical band is
# stated explicitly here to keep this script byte-reproducible.  Do not "update" it.
HISTORICAL_EXIT = ExitConvention(stop_fraction=0.30, target_fraction=0.30)


def _describe(result) -> dict:
    trades = result.trades_frame
    rej = result.rejections_frame
    out = {"eligible": result.eligible, "summary": result.summary}
    if not trades.empty:
        out["n_trades"] = int(len(trades))
        out["n_unique_entries"] = int(
            trades[["strategy_id", "signal_date", "expiry", "strike"]].drop_duplicates().shape[0])
        out["by_status"] = trades["status"].value_counts().to_dict()
        out["dte_at_entry"] = sorted(trades["dte_at_entry"].unique().tolist())
        out["option_types"] = sorted(trades["option_type"].unique().tolist())
        out["statutory_verified_true"] = int(trades["statutory_verified"].sum())
        out["brokerage_excluded_all"] = bool(trades["brokerage_excluded"].all())
        out["all_in_verified_any"] = bool(trades["all_in_verified"].any())
    if not rej.empty:
        out["n_rejections"] = int(len(rej))
        out["rejections_by_reason"] = rej["reason"].value_counts().to_dict()
    return out


def main() -> None:
    pd.set_option("display.width", 120)
    pd.set_option("display.max_columns", 50)

    # ---- 15m: primary execution resolution (persisted) ----
    primary = OptionTradeAdapter(
        AdapterConfig(resolution="15m", exit=HISTORICAL_EXIT)).run(
        persist=True, out_dir=ARTIFACTS)
    # ---- 5m: sensitivity only (not persisted as the record of truth) ----
    sensitivity = OptionTradeAdapter(
        AdapterConfig(resolution="5m", exit=HISTORICAL_EXIT)).run()

    payload = {
        "primary_15m": _describe(primary),
        "sensitivity_5m": _describe(sensitivity),
    }
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / "run_summary.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8")

    print(f"TIER1 = {TIER1}")
    print(json.dumps(payload, indent=2, default=str))


if __name__ == "__main__":
    main()
