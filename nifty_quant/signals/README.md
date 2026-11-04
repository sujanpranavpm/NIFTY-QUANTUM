# Verdict-Gated Signal Engine

This package turns validated research strategies into research-only signals.
It is **not** a live trading system and does **not** connect to any broker.

## Design principles

1. **Validation gate first.** A strategy is allowed to emit a non-`BLOCKED`
   signal only when the saved `ValidationReport` explicitly approves it.
   There is no manual override.
2. **Preserve original logic.** The engine does not re-implement entry/exit
   rules; it reruns the strategy and inspects the pending orders that the
   strategy itself submitted at the close of the last bar.
3. **No look-ahead bias.** Signals are produced from data available up to and
   including `as_of`. The earliest possible fill is the next bar.
4. **No repainting.** A signal is stamped with a deterministic fingerprint
   derived from strategy id, parameters, config, interval, symbols, start and
   end. Rerunning with the same inputs yields the same fingerprint.
5. **One signal per (strategy, symbol).** Duplicates are prevented by design.

## Signal actions

| Action  | Meaning                                                              |
|---------|----------------------------------------------------------------------|
| `BUY`   | Long-entry intent is pending.                                         |
| `SELL`  | Short-entry intent is pending (only for long-short strategies).       |
| `HOLD`  | A position is open and no exit is pending.                            |
| `EXIT`  | An exit order (stop, target, or market) is pending.                   |
| `FLAT`  | No position and no pending entry.                                     |
| `BLOCKED` | Validation did not approve the strategy; no actionable signal.       |

## Approval criteria

`DEFAULT_APPROVAL_CRITERIA` requires:

* `verdict == "supported"`
* `enough_trades` check passed
* `oos_profitable` check passed
* `strict_split` restart agrees with the sliced holdout, when tested

Any critical check failure (`enough_trades`, `oos_profitable`, `breadth`,
`fdr_significant`) is an automatic denial.

## Usage

```python
from nifty_quant.signals import SignalEngine
from nifty_quant.validation import load_validation
from nifty_quant.research.pipeline import load_market_data

validation = load_validation("artifacts/validation_smoke")
market_data = load_market_data(interval="1d", symbols=["RELIANCE", "TCS"])

engine = SignalEngine(validation)
signals = engine.generate(market_data.bars, symbols=["RELIANCE", "TCS"])

for s in signals:
    print(s.strategy_id, s.symbol, s.action, s.valid, s.validity_reason)
```

## Exports

`nifty_quant.signals.io.save_signals` writes:

* `signals.csv` - one row per signal
* `signals_metadata.json` - count and provenance

`nifty_quant.reporting.exports.export_all` additionally writes the signal frame
at the export root and under `strategies/<id>/signals.csv` when signals are
provided.

## Reproducibility

Each signal carries:

* `fingerprint` - deterministic hash of the run inputs
* `params` - the strategy parameters used
* `history` - run metadata (interval, mode, report range)
* `verdict` and `checks` - the validation outcome that gated the signal
* `as_of` / `signal_at` / `earliest_fill_at` - temporal semantics
