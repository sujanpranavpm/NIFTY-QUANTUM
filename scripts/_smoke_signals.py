"""Smoke test for the verdict-gated signal engine.

This script loads the smoke validation report, generates signals for the smoke
universe, writes sample exports, and prints a completion summary. It does not
fetch live data and does not send orders anywhere.
"""

from __future__ import annotations

from dataclasses import replace

import json
import logging
import sys
from pathlib import Path

from nifty_quant.config import PATHS
from nifty_quant.research.pipeline import load_market_data, load_results
from nifty_quant.reporting.exports import export_all
from nifty_quant.signals import SignalEngine
from nifty_quant.signals.io import save_signals
from nifty_quant.validation import load_validation

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
LOG = logging.getLogger(__name__)

SYMBOLS = ["RELIANCE", "TCS", "M_M"]
ARTIFACTS = PATHS.artifacts


def main() -> int:
    LOG.info("loading validation report from %s", ARTIFACTS / "validation_smoke")
    validation = load_validation(ARTIFACTS / "validation_smoke")

    LOG.info("loading market data for %s", SYMBOLS)
    market_data = load_market_data(interval="1d", symbols=SYMBOLS)

    LOG.info("initialising signal engine")
    engine = SignalEngine(validation)

    LOG.info("generating signals")
    signals = engine.generate(market_data.bars, symbols=SYMBOLS)
    summary = engine.summary(signals)

    signal_dir = ARTIFACTS / "signals_smoke"
    signal_dir.mkdir(parents=True, exist_ok=True)
    save_signals(signals, signal_dir, tag="smoke")
    LOG.info("wrote %d signals to %s", len(signals), signal_dir)

    results_dir = ARTIFACTS / "smoke"
    if results_dir.exists():
        LOG.info("loading research results for integrated export")
        results = load_results(results_dir)
        bundle = export_all(results, directory=signal_dir / "with_results", validation=validation, signals=signals)
        LOG.info("integrated export wrote %d files to %s", bundle.n_files, bundle.root)

    # Also generate an "approved" sample by monkeypatching orders_01 to supported.
    # This demonstrates BUY/HOLD/EXIT output without claiming real profitability.
    approved_validation = replace(
        validation,
        verdicts=validation.verdicts.copy(),
        checks=validation.checks.copy(),
    )
    mask = approved_validation.verdicts["strategy_id"] == "orders_01"
    approved_validation.verdicts.loc[mask, "verdict"] = "supported"
    approved_validation.verdicts.loc[mask, "checks_passed"] = 10
    approved_validation.verdicts.loc[mask, "checks_failed"] = 0
    approved_validation.verdicts.loc[mask, "critical_failures"] = 0
    approved_validation.verdicts.loc[mask, "failed_checks"] = ""
    approved_validation.checks.loc[approved_validation.checks["strategy_id"] == "orders_01", "passed"] = True
    approved_engine = SignalEngine(approved_validation)
    approved_signals = approved_engine.generate(market_data.bars, symbols=SYMBOLS)
    approved_dir = signal_dir / "approved_sample"
    approved_dir.mkdir(parents=True, exist_ok=True)
    save_signals(approved_signals, approved_dir, tag="approved_sample")
    LOG.info("wrote %d approved sample signals to %s", len(approved_signals), approved_dir)

    report_path = signal_dir / "signal_summary.json"
    report = {
        "engine": "verdict-gated signal engine",
        "symbols_evaluated": SYMBOLS,
        "n_signals": len(signals),
        "approval": {
            "approved": engine.approved_strategy_ids,
            "rejected": engine.rejected_strategy_ids,
        },
        "summary": summary,
    }
    report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    LOG.info("wrote summary to %s", report_path)

    print("\n=== Signal Engine Smoke Report ===")
    print(f"Symbols evaluated : {len(SYMBOLS)} ({', '.join(SYMBOLS)})")
    print(f"Total signals     : {len(signals)}")
    print(f"Approved strategies : {len(engine.approved_strategy_ids)}")
    print(f"Rejected strategies : {len(engine.rejected_strategy_ids)}")
    if summary.get("actions"):
        print("Action counts:")
        for action, count in summary["actions"].items():
            print(f"  {action}: {count}")
    print(f"Export directory  : {signal_dir}")
    print("==================================\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
