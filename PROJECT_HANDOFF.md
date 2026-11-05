# PROJECT_HANDOFF.md

Handoff notes for continuing this project in a fresh session.
Written at P0 (2026-11-04), refreshed at the P1 close-out (2026-11-04) and again at the P2
close-out (2026-11-05). Read `PROJECT_STATUS.md` first — it holds verified facts; this file holds
the operational knowledge a new session needs to not break things. For P2 specifically, read
`P2_READINESS_AUDIT.md`.

## Resume point

**Completed:** T1–T8 (platform + verdict-gated signal engine), P0 (git baseline + docs),
P1 (zero-cost NIFTY options data verification → `DATA_FEASIBILITY_REPORT.md`), and the
data-accuracy corrections milestone (volume/OI/terminal-candle/completeness + T9).
**Awaiting:** user review of the milestone report (`DATA_ACCURACY_CORRECTIONS.md`) and
the refreshed `DATA_FEASIBILITY_REPORT.md` limitations.
**Not started, and not authorised:** P2 full ingestion, T10 (full-universe research run),
T11 (strategy selection), 5m/15m candle production beyond strict-validated blocks,
dashboard/API/DB, options backtest engine, live trading.

The hard gate agreed with the user: **do not build ingestion, options backtesting,
optimisation, dashboard or broker integration until the reports are reviewed.** They are
written; the gate still stands until the review happens.

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
6. **Lot size is a per-contract-row lookup, never a constant and never only a date.**
   Measured modal lots: 75 (pre-2021-11-30, inferred), 50 (2021-12-01..2024-04-29), 25
   (2024-04-30..2024-12-19), 75 (2024-12-20..2025-12-23), 65 (2025-12-24..present).
   **61 of the 318 cached official dates carry two lots at once** (25+75 from 2024-11-22
   to 2025-01-30, 75+65 from 2025-10-30 to 2026-01-02), because a new lot applies only to
   newly issued expiries. A date-only lot is therefore a fallback and is returned with
   `estimated=True`. Re-derive with `scripts/_m2_lot_schedule.py` -> `logs/lot_schedule.txt`.
   Volume in the HF archive is share-equivalent, so contracts = volume ÷ that row's lot.
7. **Expiry weekday is not constant across history:** NIFTY weeklies and monthlies moved to
   Tuesday from 2025-09-01 (NSE/FAOP/68747). Any hard-coded "last Thursday of the month"
   is wrong for the second half of the dataset.
8. **Cloud Trader Pro is blocked by a Google sign-in wall** (`/file/d/{id}/view` → HTTP 401,
   download routes → sign-in interstitial). No bytes were obtained and no quality claim is
   made. Do not create an account to get around it.
9. **Breeze is documented, never called.** Its v2 historical endpoint exposes `1minute`,
   `5minute`, `30minute`, `1day` — **no 15-minute interval** — and needs AppKey + SecretKey +
   SessionToken from a live account.
10. **Audit-only tooling lives in `requirements-audit.txt`, not `requirements.txt`.**
    `curl_cffi` (NSE probing) and `pypdf` (circular PDF text) are there; `nifty_quant/`
    imports neither, which is the check that keeps the split honest. `polars`, `duckdb`
    and `fastparquet` are **not installed** — do not write code that assumes them.

## Test discovery

The official command is `pytest` (or `python -m pytest tests`) from the repo root; both are
verified green and also `python -m pytest` from inside `tests/`.

`pytest.ini` is what makes a bare run safe:

- `testpaths = tests` — without it a bare `pytest` from the root also collects
  `vendor/quantjourney-bt/tests/`, which imports a package (`backtester`) that is not
  installed here, and the run aborts with 8 `ModuleNotFoundError` collection errors before
  any project test runs. That was a real false-red at the P1 close-out.
- `norecursedirs = vendor .* build dist node_modules __pycache__ data artifacts logs
  reports scripts` — belt and braces for anyone who passes an explicit path.

**Nothing under `vendor/` was modified to make collection work, and nothing should be.**
`tests/conftest.py` pins the repo root onto `sys.path` so the bare and `-m` invocations
behave identically, and it is the only home for the deterministic fixtures
(`bar`/`bars`/`order`/`clock` for execution, `minute_block`/`official_eod` for data
normalisation). Core-execution tests must not read `data/` or `artifacts/`.

## Operational facts added by the data-accuracy corrections

Measured on 263 official dates / 310 file-days (`logs/m2_volume_units.txt`), 262 official
dates / 309 file-days (`logs/m3_oi_norm.txt`), 68 date×expiry blocks
(`logs/m4_terminal_candle.txt`, `logs/_m7_check.log`) and 318 official dates /
68,638 contract-rows (`logs/m5_completeness.txt`).

1. **Provider `volume` is share-equivalents** = contracts × that row's lot. Exact (summed
   absolute difference 0) on the lot-65 and mixed 65/75 file-days; median exact on lot-75
   with 0.000% non-integral bars; on the 2024 lot-25/50 dates 0.5–1.6% of minute bars are
   `lot*k+1` (an opening-minute artifact concentrated at 09:15), never a unit error.
2. **Provider `open_interest` changes scale on 2025-01-01**: share-equivalents up to
   2024-12-31, one hundredth of that from 2025-01-01. Zero dates contradict their side of
   the break. The ×100 branch is always labelled `estimated=True`.
3. **35.37% of bars carry provider OI == 0.** Those are missing observations, never filled
   and never replaced by the official end-of-day figure. Only 14,331 of 21,225 contract-days
   (67.52%) have a last-bar converted OI within 1% of the official EOD OI: that is the
   ceiling for OI-based intraday features. Intraday peak OI matches only 18.20%, so the EOD
   figure is not an intraday maximum.
4. **Timestamps are open-labelled: a bar stamped `T` covers `[T, T+1)`**, and the 15:30 bar
   is the closing/settlement print (0.038× a typical minute, 83.3% single-priced, close ==
   official `LastPric` for 100% of matched contracts). 09:15 carries the opening auction
   (5.34×, never degenerate). 12 of 68 blocks have stray labels up to 15:54 (Jan–Mar 2025
   and 2025-08-01) and are **rejected by strict validation instead of guessed**. 5- and
   15-minute candles are generated only from `STRICT_OK` blocks.
5. **Three file-days are truncated, not lost**: `2025-09-26 × 2025-09-30` stops at 15:00
   (346 of 376 labels, 9.7% of the day's volume absent — it alone supplies 724,482,825 of
   `m2`'s share residual, so it is *not* a unit error), `2025-10-21 × 2025-10-28` at 14:45 and
   `2021-05-14 × 2021-05-27` at 09:15. `completeness.py` classifies them `TRUNCATED_SESSION`,
   and a complete session whose day total is under 99% of the official one as
   `MISSING_TRADES`. **One more trap: the provider's contract coverage is partial — 41.89% of
   official contract-days have no minute bar, 20,565 of them on contracts that traded >100
   contracts.** Build any universe from the archive, never from the exchange's listing, or
   every coverage statistic becomes fiction.
6. **`TtlTrfVal` cannot arbitrate units** — for options it is a notional/strike-based figure
   (median ratio 0.0016 against the archive's own rupee value). Never use rupee turnover to
   decide a unit.
7. **The normalisation layer is `nifty_quant/derivatives/`** (`reference`, `units`,
   `timestamps`, `candles`, `completeness`). It never writes back to a raw file, never
   overwrites a provider column, and every derived number carries unit, basis, provenance
   and an `estimated` flag. Official EOD figures are attached only at or after 15:30 and
   `assert_no_eod_leakage` re-checks any frame.
8. **`scripts/_m2_volume_units.py`, `_m3_oi_norm.py` and `_m5_completeness.py` write their
   report only on a successful exit**, so a crash leaves the *previous* log in place. Read
   `logs/_m{2,3,5}_run.log` for the traceback first; never quote a stale report. Also:
   `_m5_completeness.py` must keep its per-day joins **day-scoped** (`bars_by_day` etc.) — a
   merge on `(strike, option_type)` alone fans every official row across every day in the
   file, which both corrupts the verdicts and makes the run days²-slow (it ran 6 h+ without
   finishing once; after the fix it takes ~8 min).

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

## Suggested order after this milestone's review

The corrections changed one premise: the archive's units, lot mapping, timestamp
convention and gap vocabulary are now **measured and pinned by tests**, so P2 is a scoping
decision rather than an exploration:

1. If approved, run P2 ingestion through the existing layer — `nifty_quant.derivatives`
   already does dedup-free raw preservation, the `2025-01-01` OI rescale labelled as an
   estimate, per-row historical lots with a date-regime fallback, the daily-bhavcopy
   end-of-day join behind the 15:30 visibility gate, and strict session validation. What
   P2 adds is breadth (more expiries/symbols), not new logic. An **options** cost model
   still has to be written: `costs.py` is equity-only.
2. T10 full-universe daily run to get honest equity baselines.
3. T11 screening and deep validation. Do not shortlist before T10.

Any OI-based intraday signal stays restricted to independently verified observations
(≤ 67.52% of contract-days, and never before 15:30).

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

## Files created during the data-accuracy corrections + T9

- `DATA_ACCURACY_CORRECTIONS.md` — the nine-part milestone report
- `nifty_quant/derivatives/` — `reference.py` (lot/expiry registry), `units.py` (volume/OI
  normalisation with provenance), `timestamps.py` (open-labelled convention + session
  classification), `candles.py` (strict aggregation), `completeness.py` (gap vocabulary)
- `pytest.ini` — test discovery; `requirements-audit.txt` — audit-only dependencies
- 9 re-runnable scripts `scripts/_m*.py` (incl. the three `_m2_diag*` residual-anomaly
  diagnostics), each writing its own `logs/m*_*.txt|.json`
- 8 new test files: `test_data_units`, `test_lot_reference`, `test_timestamps_candles`,
  `test_lookahead_guards`, `test_position_accounting`, `test_completeness` (231 tests, added
  for these corrections) plus `test_costs` and `test_fill_engine` (59, earlier in the same
  milestone) — 300 passing in total, on the deterministic fixtures in `tests/conftest.py`.
  The P1 baseline had only `test_signals.py`.
- `logs/_m*_run.log` — stdout/tracebacks; git-ignored like all of `logs/`

## P2 close-out (2026-11-05) — what a new session must know

- **Eight atomic commits**, one per stage, `HEAD` at the Stage-8 readiness audit; the tree is clean
  and every stage is individually revertable. `data/reference/` is tracked; `data/derived/`,
  `data/options/`, `logs/` remain git-ignored.
- **Readiness gate is the resume point.** `nifty_quant/validation/readiness.py` fails closed: a
  resolution is qualified only when all 12 gates are literally `True`. Today 11/12 pass for 1m/5m/15m
  and the open gate is `transaction_costs_verified`. The next real work (beyond P2) is to source
  official derivative-charge circulars into `data/reference/options_charge_schedule.csv` (flip those
  rates to `estimated=False`), otherwise options stay modelling-only.
- **Determinism contract.** The suite must pass from repo root, from `tests/`, bare `pytest` and
  `python -m pytest` alike (478 tests) — `tests/conftest.py` pins the repo root onto `sys.path` and
  now carries the `bracket_*` deterministic fixtures. Do not reintroduce a data-dependent skip.
- **Pinned stack.** `requirements.lock` is the reproducible contract (python 3.12.10, pandas 3.0.1,
  numpy 2.4.3, pyarrow 25.0.1, scipy 1.17.1, pytest 9.1.1); verify with `python
  scripts/_p2_env_check.py` (expect REPRODUCIBLE). Audit-only deps live in `requirements-audit.txt`.
- **Do not overclaim.** OI is always an estimate; coverage ceilings and `estimated`/`UNKNOWN` flags
  are reported, never collapsed into false certainty; no EOD look-ahead; equity engine files
  (`costs.py`, `fill_engine.py`) are untouched by design — options code is additive.
