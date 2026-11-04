# PROJECT_HANDOFF.md

Handoff notes for continuing this project in a fresh session.
Written at P0 (2026-11-04). Read `PROJECT_STATUS.md` first — it holds verified facts;
this file holds the operational knowledge a new session needs to not break things.

## Resume point

**Completed:** T1–T8 (platform + verdict-gated signal engine), P0 (git baseline + docs).
**In progress:** P1 — zero-cost NIFTY options data verification.
**Not started:** T9 (real test suite), T10 (full-universe research run), T11 (strategy
selection), dashboard/API/DB, options backtest engine.

The hard gate agreed with the user: **do not build ingestion, options backtesting,
optimisation, dashboard or broker integration until the P1 feasibility report is
reviewed.**

## Non-obvious operational facts

These are not visible from filenames and will cost time to rediscover.

1. **There is no installed package.** No `pyproject.toml`, no editable install. Any
   script run outside pytest needs:
   ```powershell
   $env:PYTHONPATH='d:\Downloads\nifty_quantum'
   ```
   `pytest` works without it only because rootdir is injected.
2. **Windows cp1252 will crash on `₹`.** Strategy `adaptation_notes` contain the
   rupee sign. Any script printing them needs `$env:PYTHONIOENCODING='utf-8'`.
   This already caused one real failure during the P0 audit.
3. **PowerShell 5.1 does not accept `&&`.** Use `;`.
4. **Long multi-line `python -c "..."` breaks badly** under PowerShell here-document
   handling. Write a temp script under `scripts/` instead — this is why
   `scripts/_audit_repo_state.py` exists.
5. **`.gitignore` intentionally versions `artifacts/smoke/*.csv` and
   `artifacts/validation_smoke/*.csv`.** The T8 tests read them. Do not "clean up"
   by ignoring all of `artifacts/` again — that silently breaks the test suite on a
   fresh clone.
6. **`vendor/quantjourney-bt` is a git-ignored upstream clone.** It is the reference
   implementation the 20 strategies were ported from. It is not part of this repo
   and is not committed.

## Interfaces you are likely to need

| Need | Call |
|---|---|
| Load cached bars | `nifty_quant.research.pipeline.load_market_data(interval="1d", symbols=[...])` → `MarketData` with `.bars` dict |
| Run one strategy | `nifty_quant.engine.backtest.run_backtest(strategy_id, bars, config=..., start=..., interval=...)` → `StrategyRun` |
| Load saved results | `nifty_quant.research.pipeline.load_results(dir_path)` |
| Load validation | `nifty_quant.validation.load_validation(dir_path)` → `ValidationReport` |
| Generate signals | `nifty_quant.signals.SignalEngine(validation).generate(bars, symbols=[...])` |
| Export everything | `nifty_quant.reporting.export_all(results, directory=..., validation=..., signals=...)` |
| Strategy metadata | `nifty_quant.strategies.registry.spec_table()` / `.applicable_ids()` / `.excluded_ids()` |

## Design rules that must survive future changes

- **Next-bar execution is the contract.** A signal computed on bar `t` fills at bar
  `t+1`. `RunSlice.pending` / `.positions` are captured *before* `engine.cancel_all()`
  precisely so this can be stated honestly.
- **The validation gate cannot be bypassed by configuration.** `DEFAULT_APPROVAL_CRITERIA`
  is the only approval path. Any new "override" flag would defeat the purpose of T8.
  Tests exercise the approved path by *copying the report object* in fixtures, never by
  weakening the gate.
- **Empty output means no file, not an empty file.** `write_frame` returns `None` for
  empty frames so readers cannot mistake a nothing-run for a computed table.
- **Never fabricate for tests.** Approved-path tests monkeypatch the *validation verdict*
  on a copied report and say so in the docstring. They do not invent prices, fills or
  performance.
- **Documented upstream quirks are preserved, not silently fixed** (e.g. `orders_16`
  missing pending-order guard, `orders_18` not calling `cancel_all`). The registry's
  `adaptation_notes` column is the record; changing behaviour requires changing the note
  and re-validating.

## Current research truth

**Zero strategies pass validation.** This is *not* evidence the strategies are weak —
it is evidence the run was a 3-symbol, 6-strategy smoke test. `breadth`,
`concentration`, `walkforward_positive` and `fdr_significant` are nearly unsatisfiable
at that sample size. No conclusion about strategy quality is admissible until a real
full-universe run exists.

## Suggested order after P1 review

1. Act on P1 verdict: if 1m options history is genuinely available → build the options
   data layer and contract reference model before touching strategies.
2. T9 test suite (offline, cheap, protects the cost model that every later number
   depends on).
3. T10 full-universe daily run to get honest equity baselines.
4. T11 screening and deep validation. Do not shortlist before step 3.

## Files created during P0

- `PROJECT_STATUS.md`, `PROJECT_HANDOFF.md`, `DATA_SOURCE_REGISTER.md`
- `scripts/_audit_repo_state.py` (read-only repo/state audit, re-runnable)
- Updated `.gitignore` (secret patterns + deliberate artifact versioning)
- Git baseline commit `b0affd5`
- `logs/audit_repo_state.txt`, `logs/pytest_baseline.txt`
