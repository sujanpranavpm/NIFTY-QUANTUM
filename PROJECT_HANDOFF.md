# PROJECT_HANDOFF.md

Handoff notes for continuing this project in a fresh session.
Written at P0 (2026-11-04), refreshed at the P1 close-out (2026-11-04). Read
`PROJECT_STATUS.md` first — it holds verified facts; this file holds the operational
knowledge a new session needs to not break things.

## Resume point

**Completed:** T1–T8 (platform + verdict-gated signal engine), P0 (git baseline + docs),
P1 (zero-cost NIFTY options data verification → `DATA_FEASIBILITY_REPORT.md`).
**Awaiting:** user review of the P1 report. Nothing beyond P1 is authorised.
**Not started:** T9 (real test suite), T10 (full-universe research run), T11 (strategy
selection), dashboard/API/DB, options backtest engine.

The hard gate agreed with the user: **do not build ingestion, options backtesting,
optimisation, dashboard or broker integration until the P1 feasibility report is
reviewed.** P1 is now written; the gate still stands until the review happens.

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

## Operational facts added by P1

1. **`logs/` and `data/raw/` are git-ignored and must stay that way.** All 64 P1 log
   artifacts are the evidence base for the report; a fresh clone will not contain them.
   If a number in `DATA_FEASIBILITY_REPORT.md` needs re-checking, re-run the named
   `scripts/_p1_*.py`, do not retype it.
2. **Two HTTP routes, both measured.** `nsearchives.nseindia.com` static files (bhavcopy
   ZIP, circular PDFs) answer plain `urllib` with a browser User-Agent — `FAOP68747.pdf`
   returned 200/181,366 bytes that way (`logs/p1_nse_reachability.txt`). `www.nseindia.com`
   API pages were fetched with `curl_cffi` (`impersonate="chrome124"` + warm-up GET on the
   homepage and `/option-chain`), which is the convention this repo follows; **no control
   test proved a plain client is rejected there**, and the only 403/`Access Denied` observed
   in P1 was the BSE quote API. Do not repeat an NSE fetch without reading the status code.
3. **The free official bhavcopy begins 2024-01-02.** Do not attempt 2021–2023 verification
   against it — it is a 404, not a bug in the code. A `Range: bytes=0-3` request returning
   `PK` is the cheapest trading-day test (404 = holiday).
4. **Compare `LastPric`, never `ClsPric`, with an archive close.** `ClsPric` agrees with the
   daily close on only 3.89–43.04% of contracts and `SttlmPric` is a derived value whose
   median gap to an option premium is ≈ the whole underlying.
5. **NIFTY option premiums are on the ₹0.05 tick** despite the published contract-specs slab
   that would put them on ₹0.10 above index 15,000. Two independent observations beat the
   rule text; the contradiction is recorded in `DATA_SOURCE_REGISTER.md` §3 and must not be
   silently re-litigated.
6. **Lot size is a date lookup, never a constant:** `NewBrdLotQty` was 50 (2024-01→04), 25
   (from 2024-05), 75 (from 2024-12), 65 (from 2025-11). Volume in the HF archive is
   share-equivalent, so contracts = volume ÷ that date's lot size.
7. **Expiry weekday is not constant across history:** NIFTY weeklies and monthlies moved to
   Tuesday from 2025-09-01 (NSE/FAOP/68747). Any hard-coded "last Thursday of the month"
   is wrong for the second half of the dataset.
8. **Cloud Trader Pro is blocked by a Google sign-in wall** (`/file/d/{id}/view` → HTTP 401,
   download routes → sign-in interstitial). No bytes were obtained and no quality claim is
   made. Do not create an account to get around it.
9. **Breeze is documented, never called.** Its v2 historical endpoint exposes `1minute`,
   `5minute`, `30minute`, `1day` — **no 15-minute interval** — and needs AppKey + SecretKey +
   SessionToken from a live account.
10. **PDF text extraction uses `pypdf`; NSE probing uses `curl_cffi`.** Both are now listed
    under "P1 data-audit tooling" in `requirements.txt`. `polars`, `duckdb` and `fastparquet`
    are **not installed** — do not write code that assumes them.
11. **Run the suite as `python -m pytest tests`, never bare `pytest`.** There is no
    `pytest.ini`/`pyproject.toml`, so a bare run collects `vendor/quantjourney-bt/tests/`
    too and aborts with 8 `ModuleNotFoundError: No module named 'backtester'` collection
    errors before running anything (`Interrupted: 8 errors during collection`). This cost a
    false red at the P1 close-out; the suite itself is green.

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

P1's verdict changes step 1 from a conditional to a scoping decision, because 1m option
**prices** are genuinely available while **OI and depth are not**:

1. If approved, build the options data layer exactly as `DATA_FEASIBILITY_REPORT.md` §F
   specifies — dedup, session filter, the `2025-01-01` OI rescale labelled as an estimate,
   date-driven lot sizes, and the daily-bhavcopy join for authoritative end-of-day OI —
   then the contract reference model and an **options** cost model (the current `costs.py`
   is equity-only). Strategy work comes after that, and any OI-based signal must be
   restricted to 2024-onwards daily OI.
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

## Files created during P1

- `DATA_FEASIBILITY_REPORT.md` — sections A–F, the P1 deliverable
- `DATA_SOURCE_REGISTER.md` — completed (it had been listed in P0 but never written)
- 37 re-runnable audit scripts `scripts/_p1_*.py` (each one writes its own log)
- `logs/p1_*` — 64 `.txt|.json` artifacts; git-ignored, regenerate by running the scripts
- `data/raw/p1_*` — cached bhavcopies, circulars, page HTML, Breeze docs; git-ignored
