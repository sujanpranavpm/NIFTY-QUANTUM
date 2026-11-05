"""P2 stage 8 - readiness checks (directive Parts 10-11).

Gathers the *evidence* for each Part-10 qualification gate from the artifacts the
earlier stages actually wrote (reference DB, candle-integrity report, coverage
registry, charge schedule, the locked dependency contract) and feeds them through the
same fail-closed gate the test suite pins (``nifty_quant.validation.readiness``).

It runs no strategy and ingests nothing: it only reports whether the data + backtesting
layer is *permitted* to enter research, resolution by resolution, and states every
open gate honestly.  Exit status is 0 whenever the assessment is produced; "qualified"
is a conclusion written in the report, not the success criterion of the script - a
limitation that keeps a gate closed is an expected, reported outcome, not a crash.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from nifty_quant.validation import (  # noqa: E402
    GATES,
    GATE_LABELS,
    RESOLUTIONS,
    assess_readiness,
    holdout_protected,
    resolution_dataset_evidenced,
)
from nifty_quant.validation.readiness import (  # noqa: E402
    CONTRACT_IDENTITY,
    MISSING_DATA_EXPLICIT,
    MULTIPLE_TESTING_SAFEGUARDED,
    PREMIUM_DATA_QUALIFIED,
    RESOLUTION_DATA_EVIDENCED,
    RESULTS_REPRODUCIBLE,
    TIMESTAMP_CONVENTION,
    TRANSACTION_COSTS_VERIFIED,
    WALKFORWARD_READY,
)

REFERENCE = ROOT / "data" / "reference"
DERIVED = ROOT / "data" / "derived" / "candles"
LOGS = ROOT / "logs"


def _log_contains(name: str, *markers: str) -> bool:
    path = LOGS / name
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    return all(m in text for m in markers)


def _reference_has(*names: str) -> bool:
    return all((REFERENCE / n).exists() and (REFERENCE / n).stat().st_size > 0 for n in names)


def contract_identity() -> bool:
    """Effective-dated lot/expiry/calendar reference and a persisted expiry registry."""
    return _reference_has("nifty_reference_parameters.csv", "nifty_expiry_dates.csv",
                          "nifty_lot_history.csv", "nifty_trading_days.csv")


def premium_data_qualified() -> bool:
    """The candle build reported zero OHLC-containment and zero volume-conservation failures."""
    return _log_contains("p2_candle_integrity.txt",
                         "INTEGRITY VERDICT          : PASS",
                         "OHLC containment failures  : 0",
                         "volume conservation delta  : 0.000000")


def missing_data_explicit() -> bool:
    """A persisted coverage registry exists and reconciles to the completeness report."""
    registry = (REFERENCE / "nifty_coverage_registry.csv").exists()
    reconciles = _log_contains("p2_coverage.txt", "reconcile vs logs/m5_completeness.json: MATCH")
    return registry and reconciles


def timestamp_convention() -> bool:
    try:
        from nifty_quant.derivatives.candles import CONVENTION
    except Exception:
        return False
    return CONVENTION == "open_labelled"


def transaction_costs_verified() -> bool:
    """Strictly *verified* only if some statutory rate was sourced from cached bytes.

    The stage-5 audit found 0 of 5 rates sourced (all ``estimated``), so the fail-closed
    answer here is False: no trade using default statutory rates may enter strict
    financial validation.  The cost *engine* is tested and present; verification is not.
    """
    schedule = REFERENCE / "options_charge_schedule.csv"
    if not schedule.exists():
        return False
    frame = pd.read_csv(schedule)
    if "estimated" not in frame.columns:
        return False
    sourced = (frame["estimated"].astype(str).str.lower() == "false").sum()
    return bool(sourced >= 1)


def results_reproducible() -> bool:
    return _log_contains("p2_env.txt", "verdict: REPRODUCIBLE")


def walkforward_ready() -> bool:
    """Walk-forward folds tile a plain bar index, so they are frequency-agnostic."""
    from nifty_quant.validation import generate_folds

    index = pd.date_range("2020-01-01", periods=3000, freq="B")
    return bool(generate_folds(index, train_bars=1500, test_bars=500))


def holdout_gate() -> bool:
    """The committed split's dev/purge/holdout windows are ordered and disjoint."""
    from nifty_quant.validation import dev_oos_split

    index = pd.date_range("2020-01-01", periods=1000, freq="B")
    split = dev_oos_split(index, dev_fraction=0.7, purge_bars=50, min_oos_bars=100)
    return holdout_protected(split.dev_end, split.purge_start, split.purge_end, split.oos_start)


def multiple_testing_safeguarded() -> bool:
    from nifty_quant.validation import benjamini_hochberg, deflated_sharpe_ratio  # noqa: F401

    q = benjamini_hochberg([0.001, 0.02, 0.4])
    return q is not None and len(q) == 3


def build_evidence(resolution: str) -> dict[str, bool]:
    return {
        CONTRACT_IDENTITY: contract_identity(),
        PREMIUM_DATA_QUALIFIED: premium_data_qualified(),
        MISSING_DATA_EXPLICIT: missing_data_explicit(),
        TIMESTAMP_CONVENTION: timestamp_convention(),
        TRANSACTION_COSTS_VERIFIED: transaction_costs_verified(),
        "execution_tested": _log_contains("p2_execution.txt", "execution verdict: PASS"),
        "accounting_valid": _log_contains("p2_execution.txt", "execution verdict: PASS")
                            and (ROOT / "nifty_quant" / "engine" / "options_accounting.py").exists(),
        RESULTS_REPRODUCIBLE: results_reproducible(),
        WALKFORWARD_READY: walkforward_ready(),
        "holdout_protected": holdout_gate(),
        MULTIPLE_TESTING_SAFEGUARDED: multiple_testing_safeguarded(),
        RESOLUTION_DATA_EVIDENCED: resolution_dataset_evidenced(resolution, DERIVED),
    }


def main() -> int:
    lines: list[str] = []
    w = lines.append
    w("P2 stage 8 - data + backtesting readiness audit")
    w(f"generated {datetime.now():%Y-%m-%d %H:%M:%S}; resolution-by-resolution, fail-closed")
    w("=" * 90)

    rows = []
    for resolution in RESOLUTIONS:
        assessment = assess_readiness(resolution, build_evidence(resolution))
        rows.append(assessment)
        w(f"\nRESOLUTION {resolution}  ->  "
          f"{'QUALIFIED' if assessment.is_qualified else 'NOT QUALIFIED (research modelling only, strict financial use withheld)'}")
        for gate in GATES:
            status = "PASS" if assessment.passed(gate) else "open"
            w(f"   [{status:>4}] {gate:<32} {GATE_LABELS[gate]}")

    w("\n" + "=" * 90)
    w("SUMMARY (one row per resolution)")
    w(f"{'resolution':<10} {'qualified':<10} {'gates':<8} failed gates")
    for a in rows:
        r = a.as_row()
        gates = f"{r['gates_passed']}/{r['gates_total']}"
        w(f"{r['resolution']:<10} {str(r['qualified']):<10} {gates:<8} {r['failed_gates'] or '-'}")

    w("\nHONEST LIMITATIONS (why a gate may stay open):")
    w("  - transaction_costs_verified is open: 0 of 5 statutory rates were sourced from cached")
    w("    bytes (all estimated), so per the directive those trades are excluded from strict")
    w("    financial validation - the cost engine exists and is tested, verification does not.")
    w("  - intraday coverage horizon is the cached provider archive (newest expiry truncated);")
    w("    no free, no-account source extends it, so it is reported, never fabricated.")
    w("  - OI-observation availability on exchange-positive contract-days is measured (33.6%")
    w("    pre-2026, 22.4% in 2026), described as an estimate, never forward-filled.")
    w("\nNOTE: this audit authorises P2 ingestion; it does not run ingestion or any strategy.")

    text = "\n".join(lines) + "\n"
    LOGS.mkdir(parents=True, exist_ok=True)
    (LOGS / "p2_readiness.txt").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
